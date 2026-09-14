"""Target-specific packaging regressions for the unified asset packager."""
from __future__ import annotations

import io
import os
import sys
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.abspath(os.path.join(_HERE, "..", "src"))):
    if _path not in sys.path:
        sys.path.insert(0, _path)


class _FakeS3:
    def __init__(self):
        self.payload = None

    def upload_fileobj(self, fileobj, _bucket, _key, ExtraArgs=None):
        self.payload = fileobj.read()

    def generate_presigned_url(self, *_args, **_kwargs):
        return "https://example.test/aicc.zip"


def _bundle():
    return {
        "flows": [{"flowId": "MainFlow", "nodes": {}}],
        "slot_types": [{"slotTypeId": "OrderStatus"}],
        "context_variables": [],
        "data_requests": [{
            "dataRequestId": "getOrder",
            "webhook": {"implementation": "external", "url": "{WEBHOOK_URL}/tools/get_order"},
        }],
        "knowledge_bases": [{"name": "FAQ", "articles": []}],
        "guardrails": [{"name": "PII"}],
        "secrets": [{"name": "ApiToken", "description": "runtime only", "valueEnv": "ACXD_SECRET_API_TOKEN"}],
        "application": {"name": "acme-refunds"},
        "contact_flows": [{"name": "Inbound", "Metadata": {"acxdBinding": {}}}],
    }


def _package(monkeypatch, *, acxd: bool, d9=None, extra_contents=None):
    import tools.asset_packager as packager

    session_id = "session-1"
    contents = {
        f"assets/{session_id}/infrastructure/infrastructure.yaml": "Resources: {}\n",
        f"assets/{session_id}/lambda/get_order/index.py": "def handler(event, context): return {}\n",
        f"assets/{session_id}/contact_flow/contact_flow.json": '{"Version":"2019-10-30","Actions":[]}',
        f"assets/{session_id}/prompt/ai_agent_prompt.yaml": "system: classic\n",
    }
    contents.update(extra_contents or {})
    client = _FakeS3()
    monkeypatch.setattr(packager, "_is_acxd_target", lambda _session: acxd)
    monkeypatch.setattr(packager, "_load_acxd_bundle", lambda _session: _bundle())
    monkeypatch.setattr(packager, "_run_d9_checks", lambda _session, _bundle_value: list(d9 or []))
    monkeypatch.setattr(packager, "get_bucket_name", lambda: "test-bucket")
    monkeypatch.setattr(packager, "get_s3_client", lambda: client)
    monkeypatch.setattr(packager, "list_session_assets", lambda *_args, **_kwargs: list(contents))
    monkeypatch.setattr(packager, "get_asset_from_s3", lambda key, **_kwargs: contents.get(key))

    result = packager.package_assets_impl(session_id, "acme-refunds")
    return result, client


def test_acxd_zip_contains_runtime_assets_and_omits_classic_prompt(monkeypatch):
    result, client = _package(monkeypatch, acxd=True)

    assert result["success"] is True
    assert result["runtime_target"] == "acxd"
    assert client.payload
    with zipfile.ZipFile(io.BytesIO(client.payload)) as archive:
        names = set(archive.namelist())
    root = "acme-refunds/"
    assert {
        root + "assets/acxd/flows/MainFlow.json",
        root + "assets/acxd/slot-types/OrderStatus.json",
        root + "assets/acxd/data-requests/getOrder.json",
        root + "assets/acxd/guardrails/PII.json",
        root + "assets/acxd/knowledge-bases/FAQ.json",
        root + "assets/acxd/secrets/ApiToken.json",
        root + "assets/acxd/context-variables.json",
        root + "assets/acxd/application.json",
        root + "deploy-manifest.json",
        root + "runner.js",
        root + "package.json",
        root + "lib/steps.js",
        root + "WIRING-GUIDE.md",
        root + "contact-flow/contact_flow.json",
    }.issubset(names)
    assert root + "prompts/ai_agent_prompt.yaml" not in names


def test_classic_zip_keeps_its_existing_layout(monkeypatch):
    result, client = _package(monkeypatch, acxd=False)

    assert result["success"] is True
    assert result["runtime_target"] == "classic"
    with zipfile.ZipFile(io.BytesIO(client.payload)) as archive:
        names = set(archive.namelist())
    root = "acme-refunds/"
    assert root + "prompts/ai_agent_prompt.yaml" in names
    assert root + "contact-flow/contact_flow.json" in names
    assert not any(name.startswith(root + "assets/acxd/") for name in names)
    assert root + "runner.js" not in names


def test_acxd_packaging_refuses_every_d9_violation(monkeypatch):
    result, client = _package(monkeypatch, acxd=True, d9=["D9-3: Data Request getOrder is absent from OpenAPI"])

    assert result["success"] is False
    assert result["problems"] == ["D9: D9-3: Data Request getOrder is absent from OpenAPI"]
    assert client.payload is None


def test_manifest_coverage_requires_canonical_dependency_order():
    from tools.acxd_manifest_builder import build_manifest, check_manifest_coverage

    bundle = _bundle()
    manifest = build_manifest(bundle, project_name="acme-refunds")
    assert check_manifest_coverage(manifest, bundle) == []

    flow_index = next(i for i, step in enumerate(manifest["steps"])
                      if step["type"] == "upsert-flows")
    guardrail_index = next(i for i, step in enumerate(manifest["steps"])
                           if step["type"] == "upsert-guardrails")
    manifest["steps"][flow_index], manifest["steps"][guardrail_index] = (
        manifest["steps"][guardrail_index], manifest["steps"][flow_index])
    assert any("upsert-guardrails" in problem and "upsert-flows" in problem
               for problem in check_manifest_coverage(manifest, bundle))


def test_acxd_filter_downloads_the_application_in_its_bundle_layout(monkeypatch):
    """Progress panel 'ACXD Application' download (QA 2026-09-11): `asset_type=acxd`
    used to match nothing (404). It must select every stored ACXD resource and
    keep the assets/acxd/ layout of the full bundle, without Classic files."""
    import tools.asset_packager as packager

    session_id = "session-2"
    contents = {
        f"assets/{session_id}/lambda/get_order/index.py": "def handler(event, context): return {}\n",
        f"assets/{session_id}/acxd_flow/MainFlow.json": '{"flowId":"MainFlow","nodes":{}}',
        f"assets/{session_id}/acxd_slot_type/OrderStatus.json": '{"slotTypeId":"OrderStatus"}',
        f"assets/{session_id}/acxd_data_request/getOrder.json": '{"dataRequestId":"getOrder"}',
        f"assets/{session_id}/acxd_application/application.json": '{"name":"acme-refunds"}',
        f"assets/{session_id}/acxd_context_variable/context-variables.json": '[]',
    }
    client = _FakeS3()
    monkeypatch.setattr(packager, "_is_acxd_target", lambda _session: True)
    monkeypatch.setattr(packager, "get_bucket_name", lambda: "test-bucket")
    monkeypatch.setattr(packager, "get_s3_client", lambda: client)
    monkeypatch.setattr(packager, "list_session_assets", lambda *_args, **_kwargs: list(contents))
    monkeypatch.setattr(packager, "get_asset_from_s3", lambda key, **_kwargs: contents.get(key))

    result = packager.package_assets_impl(session_id, "acme-refunds", asset_type_filter="acxd", include_readme=False)

    assert result["success"], result
    names = set(zipfile.ZipFile(io.BytesIO(client.payload)).namelist())
    # deploy.sh rides along with every download (existing behaviour); nothing else may.
    assert names - {"acme-refunds/deploy.sh"} == {
        "acme-refunds/assets/acxd/flows/MainFlow.json",
        "acme-refunds/assets/acxd/slot-types/OrderStatus.json",
        "acme-refunds/assets/acxd/data-requests/getOrder.json",
        "acme-refunds/assets/acxd/application.json",
        "acme-refunds/assets/acxd/context-variables.json",
    }


def test_identical_duplicate_copy_is_packaged_once(monkeypatch):
    """Live (SELC, 2026-09-13): the preview stream had saved research.json twice
    (research/research.json and research/selc-research/research.json); both map
    to aicc-poc/research/research.json and the packager refused the bundle as
    ambiguous. An identical copy is the same file — only differing content is."""
    doc = '{"topic": "cleaning"}'
    result, client = _package(monkeypatch, acxd=True, extra_contents={
        "assets/session-1/research/research.json": doc,
        "assets/session-1/research/selc-research/research.json": doc,
    })
    assert result["success"] is True, result
    with zipfile.ZipFile(io.BytesIO(client.payload)) as archive:
        assert archive.namelist().count("acme-refunds/research/research.json") == 1


def test_differing_duplicate_copy_is_refused(monkeypatch):
    result, _client = _package(monkeypatch, acxd=True, extra_contents={
        "assets/session-1/research/research.json": '{"v": 1}',
        "assets/session-1/research/selc-research/research.json": '{"v": 2}',
    })
    assert result["success"] is False
    assert "different content" in (result.get("error") or "")


def test_acxd_lambda_handlers_get_the_format_restorer(monkeypatch):
    import tools.asset_packager as packager
    monkeypatch.setattr(packager, "_acxd_field_patterns",
                        lambda folder: {"phoneNumber": r"^010-\d{4}-\d{4}$"} if folder == "get_order" else {})
    result, client = _package(monkeypatch, acxd=True)
    assert result["success"] is True
    with zipfile.ZipFile(io.BytesIO(client.payload)) as archive:
        code = archive.read("acme-refunds/lambda/get_order/index.py").decode("utf-8")
    assert "ACXD delivers slot values without separators" in code
    assert "_aicc_restore_formats" in code and "def lambda_handler(event, context)" in code


def test_classic_lambda_handlers_are_left_alone(monkeypatch):
    import tools.asset_packager as packager
    monkeypatch.setattr(packager, "_acxd_field_patterns", lambda folder: {"phoneNumber": r"^010-\d{4}-\d{4}$"})
    result, client = _package(monkeypatch, acxd=False)
    with zipfile.ZipFile(io.BytesIO(client.payload)) as archive:
        code = archive.read("acme-refunds/lambda/get_order/index.py").decode("utf-8")
    assert "ACXD delivers slot values" not in code
