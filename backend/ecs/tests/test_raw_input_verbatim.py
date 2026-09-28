"""raw_input is stored from the customer's own bytes, not the model's re-typed copy.

e2e 2026-09-25 (SELC, ACXD only): a 13.9 KB uploaded document reached the
workspace as a 7.6 KB summary the model wrote, and a quoted hand-off sentence
("이전 전달주신 정보는 …") was reworded on its way into the plan.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
for _path in (_SRC, os.path.abspath(os.path.join(_HERE, ".."))):
    if _path not in sys.path:
        sys.path.insert(0, _path)

DOC = ("# PoC\n" + "상담사를 연결해드리겠습니다. 이전 전달주신 정보는 상담사에게 자동 전달됩니다.\n" * 12)


def _uploads(monkeypatch, tmp_path, files: dict):
    import tools.workspace_file_tools as wft
    root = tmp_path / "session"
    (root / "uploads").mkdir(parents=True)
    for name, text in files.items():
        (root / "uploads" / name).write_text(text, encoding="utf-8")
    monkeypatch.setattr(wft, "_resolve_safe_path", lambda sid, rel: root / rel)


def test_an_uploaded_text_document_is_stored_verbatim(monkeypatch, tmp_path):
    from tools import project_workspace as pw
    _uploads(monkeypatch, tmp_path, {"doc.md": DOC, "diagram.png": "not text"})
    stored, source = pw._verbatim_raw_input("요약본: 상담사를 연결해 드립니다.", "session-x")
    assert source == "customer_text" and stored == DOC.strip()


def test_a_pasted_document_message_is_stored_verbatim(monkeypatch, tmp_path):
    from tools import project_workspace as pw
    from tools.session_context import current_user_message_text
    _uploads(monkeypatch, tmp_path, {})
    token = current_user_message_text.set(DOC)
    try:
        stored, source = pw._verbatim_raw_input("짧게 줄인 문서", "session-x")
    finally:
        current_user_message_text.reset(token)
    assert source == "customer_text" and stored == DOC.strip()


def test_the_model_text_is_kept_without_a_customer_document_or_when_it_merged_more(monkeypatch, tmp_path):
    from tools import project_workspace as pw
    from tools.session_context import current_user_message_text
    _uploads(monkeypatch, tmp_path, {})
    token = current_user_message_text.set("네, 그렇게 해 주세요.")
    try:
        assert pw._verbatim_raw_input("모델이 모은 문서", "session-x") == ("모델이 모은 문서", "model_text")
    finally:
        current_user_message_text.reset(token)
    _uploads(monkeypatch, tmp_path / "b", {"doc.md": DOC})
    merged = DOC * 3                      # several messages merged by the model
    assert pw._verbatim_raw_input(merged, "session-x") == (merged, "model_text")
