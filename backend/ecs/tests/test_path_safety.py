"""Request data that becomes a path segment is allow-listed, never patched."""
import os
from pathlib import Path

from tools.path_safety import path_under, safe_segment


def test_safe_segment_accepts_plain_names_and_rejects_everything_else():
    for ok in ("b12d87e3-7d5b-41b3-b851-05b6196efe5f", "test-1", "flows", "deliveryLookup.json", "a_b.c"):
        assert safe_segment(ok) == ok
    for bad in ("", None, ".", "..", "../x", "a/b", "a\\b", "a b", "x\x00y", "é", "a" * 129, ". ", "a;b"):
        assert safe_segment(bad) is None, bad


def test_path_under_stays_inside_the_base(tmp_path):
    base = tmp_path / "sessions"
    assert path_under(base, "sid-1", "assets") == Path(os.path.normpath(str(base / "sid-1" / "assets")))
    assert path_under(base, "../etc", "assets") is None
    assert path_under(base, "sid-1", "..") is None
    assert path_under(base) is None


def test_acxd_state_and_asset_roots_refuse_an_unsafe_session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(tmp_path))
    from tools import acxd_bundle, acxd_flow_spec, acxd_asset_patcher
    assert acxd_flow_spec._state_dir("../../etc") is None
    assert acxd_bundle._assets_root("a/b") is None
    assert acxd_asset_patcher._assets_root("..") is None
    good = acxd_flow_spec._state_dir("sid-1")
    assert good == tmp_path / "sessions" / "sid-1" / "state"
    # an asset type with a separator never reaches the filesystem
    (tmp_path / "sessions" / "sid-1" / "assets" / "flows").mkdir(parents=True)
    (tmp_path / "sessions" / "sid-1" / "assets" / "flows" / "f.json").write_text('{"flowId": "F"}', encoding="utf-8")
    assert acxd_bundle._read_json_docs("sid-1", "flows") == [{"flowId": "F"}]
    assert acxd_bundle._read_json_docs("sid-1", "../state") == []


def test_ai_prompt_variable_pattern_is_linear_on_hostile_input():
    import time
    from tools.asset_linters import _AI_PROMPT_VAR_RE
    hostile = "{{{{" + " " * 20000
    started = time.perf_counter()
    assert _AI_PROMPT_VAR_RE.search(hostile) is None
    assert time.perf_counter() - started < 0.5
    assert [m.group(1).strip() for m in _AI_PROMPT_VAR_RE.finditer("a {{ $.x }} b {{$.Custom.y}}")] == ["$.x", "$.Custom.y"]


def test_bundle_load_failure_does_not_leak_the_exception_text(monkeypatch):
    from tools import validate_acxd_consistency as vac
    def boom(*a, **k):
        raise RuntimeError("/mnt/s3/sessions/x: Permission denied (secret path)")
    monkeypatch.setattr(vac, "_load_acxd_validation_inputs", boom)
    out = vac.validate_acxd_consistency(session_id="sid-1")
    assert [v.code for v in out] == ["BUNDLE_LOAD_FAILED"]
    assert "Permission denied" not in out[0].message and "see the server log" in out[0].message
