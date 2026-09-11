"""One home per asset file: workspace writes never create a sibling copy, bundles never ship two."""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.abspath(os.path.join(_HERE, "..", "src"))):
    if _path not in sys.path:
        sys.path.insert(0, _path)


def test_root_asset_path_redirects_to_the_single_operation_copy(tmp_path):
    from tools.workspace_file_tools import _canonical_asset_path
    root = tmp_path
    (root / "assets" / "contact_flow" / "selc-inbound-main-flow").mkdir(parents=True)
    (root / "assets" / "contact_flow" / "selc-inbound-main-flow" / "contact_flow.json").write_text("{}")
    assert _canonical_asset_path(root, "assets/contact_flow/contact_flow.json") == \
        "assets/contact_flow/selc-inbound-main-flow/contact_flow.json"
    # an explicit operation path, a non-asset path and an existing root file are untouched
    assert _canonical_asset_path(root, "assets/contact_flow/other/contact_flow.json") == \
        "assets/contact_flow/other/contact_flow.json"
    assert _canonical_asset_path(root, "workspace/notes.md") == "workspace/notes.md"
    (root / "assets" / "contact_flow" / "contact_flow.json").write_text("{}")
    assert _canonical_asset_path(root, "assets/contact_flow/contact_flow.json") == "assets/contact_flow/contact_flow.json"


def test_ambiguous_targets_are_not_guessed(tmp_path):
    from tools.workspace_file_tools import _canonical_asset_path
    for op in ("a", "b"):
        (tmp_path / "assets" / "lambda" / op).mkdir(parents=True)
        (tmp_path / "assets" / "lambda" / op / "handler.py").write_text("")
    assert _canonical_asset_path(tmp_path, "assets/lambda/handler.py") == "assets/lambda/handler.py"


def test_bundle_keeps_one_contact_flow_per_name():
    from tools.acxd_bundle import _dedupe_contact_flows
    older = {"Name": "selc-inbound-main-flow", "Version": "2019-10-30", "Actions": [{"Identifier": "a"}]}
    newer = {"Name": "selc-inbound-main-flow", "Version": "2019-10-30", "Actions": [{"Identifier": "a"}, {"Identifier": "b"}]}
    other = {"Name": "outbound", "Version": "2019-10-30", "Actions": []}
    kept = _dedupe_contact_flows([older, newer, other, json.loads(json.dumps(other))])
    assert kept == [newer, other]
