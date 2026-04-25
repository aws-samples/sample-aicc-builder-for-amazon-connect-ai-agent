"""Tests for context.modification_tracking."""

from __future__ import annotations

import os
import sys

import pytest

# Make ``src`` importable the same way the Docker entrypoint does.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from context.modification_tracking import (  # noqa: E402
    normalize_keywords,
    record_modification_request,
    record_modification_outcome,
    reset_repeat_counter,
    format_modification_state_block,
    suggest_placeholder,
    ASSET_LABELS,
)


@pytest.fixture(autouse=True)
def _isolated_nfs(tmp_path, monkeypatch):
    """Redirect the module's NFS state path to a temp dir for every test."""
    monkeypatch.setenv("S3FILES_MOUNT_PATH", str(tmp_path))
    yield


def test_normalize_keywords_flow_korean():
    assert normalize_keywords("flow에서 첫 멘트 바꿔줘") == ["greeting_ambiguous", "contact_flow"]


def test_normalize_keywords_prompt_english():
    assert "prompt" in normalize_keywords("please edit the prompt")


def test_normalize_keywords_infra():
    assert "infrastructure" in normalize_keywords("CloudFormation 수정")


def test_normalize_keywords_empty():
    assert normalize_keywords("") == []
    assert normalize_keywords("그냥 뭐라 쓰고") == []


def test_normalize_keywords_greeting_is_ambiguous_first():
    # "인사말" must resolve to greeting_ambiguous, not contact_flow
    out = normalize_keywords("인사말 수정해줘")
    assert out[0] == "greeting_ambiguous"


def test_record_bumps_repeat_counter():
    state = record_modification_request("sess-1", "flow에서 멘트 바꿔")
    assert state["repeat_counter"]["contact_flow"] == 1
    state = record_modification_request("sess-1", "flow 한 번 더")
    assert state["repeat_counter"]["contact_flow"] == 2


def test_record_outcome_annotates_latest():
    record_modification_request("sess-2", "prompt 수정")
    record_modification_outcome("sess-2", "claimed_success", target_asset="prompt")
    from context.modification_tracking import _read_state
    state = _read_state("sess-2")
    assert state["recent_requests"][-1]["outcome"] == "claimed_success"
    assert state["recent_requests"][-1]["target_asset"] == "prompt"


def test_reset_repeat_counter():
    record_modification_request("sess-3", "flow 수정")
    record_modification_request("sess-3", "flow 수정 재차")
    reset_repeat_counter("sess-3", "contact_flow")
    from context.modification_tracking import _read_state
    assert _read_state("sess-3")["repeat_counter"]["contact_flow"] == 0


def test_format_state_block_emits_repeat_warning():
    record_modification_request("sess-4", "flow 수정")
    record_modification_request("sess-4", "flow 또 수정")
    record_modification_outcome("sess-4", "claimed_success", target_asset="contact_flow")
    # Third turn with same keyword — the block should now mention repeats
    block = format_modification_state_block("sess-4", "flow 진짜 수정해달라니까")
    assert block is not None
    assert "Repeat counters" in block
    assert "contact_flow" in block
    assert ASSET_LABELS["contact_flow"] in block


def test_format_state_block_none_when_no_activity():
    block = format_modification_state_block("sess-empty", "hello there")
    # No keywords in input, no prior state → block should be None
    assert block is None


def test_format_state_block_shows_current_parse_only():
    block = format_modification_state_block("sess-5", "flow 수정")
    assert block is not None
    assert "Current turn" in block
    assert "contact_flow" in block
    assert "Repeat counters" not in block  # counter = 0 at parse time


# ── suggest_placeholder matrix ────────────────────────────────────────

def test_suggest_placeholder_fallback_when_unknown_phase():
    ko = suggest_placeholder("sess-p0", "", phase=None, language="ko-KR")
    en = suggest_placeholder("sess-p0", "", phase=None, language="en-US")
    assert ko == "메시지를 입력하세요"
    assert en == "Type a message"


def test_suggest_placeholder_interview_rotates():
    # len(recent) == 0 on a fresh session, so we get the first rotation item
    first_ko = suggest_placeholder("sess-p1", "", phase="interview", language="ko")
    first_en = suggest_placeholder("sess-p1", "", phase="interview", language="en")
    assert "호텔" in first_ko
    assert "hotel" in first_en.lower()


def test_suggest_placeholder_generation_hint():
    ko = suggest_placeholder("sess-p2", "", phase="generation", language="ko")
    en = suggest_placeholder("sess-p2", "", phase="generation", language="en")
    assert "계속" in ko
    assert "continue" in en.lower() or "proceed" in en.lower()


def test_suggest_placeholder_post_generation_gives_edit_example():
    ko = suggest_placeholder("sess-p3", "", phase="post_generation", language="ko")
    en = suggest_placeholder("sess-p3", "", phase="post_generation", language="en")
    assert "수정" in ko or "contact_flow" in ko
    assert "Edit" in en or "contact_flow" in en


def test_suggest_placeholder_ambiguous_greeting_takes_priority():
    # Phase says post_generation but user mentioned "인사말" → ambiguous copy wins
    ko = suggest_placeholder(
        "sess-p4",
        current_user_text="인사말 바꿔줘",
        phase="post_generation",
        language="ko",
    )
    en = suggest_placeholder(
        "sess-p4",
        current_user_text="change the greeting",
        phase="post_generation",
        language="en",
    )
    assert "contact_flow" in ko and "ai_agent_prompt" in ko
    assert "contact_flow" in en and "ai_agent_prompt" in en


def test_suggest_placeholder_repeat_correction_highest_priority():
    # Build up repeat_counter >= 2 with last outcome claimed_success
    record_modification_request("sess-p5", "flow 수정")
    record_modification_request("sess-p5", "flow 또 수정")
    record_modification_outcome("sess-p5", "claimed_success", target_asset="contact_flow")
    ko = suggest_placeholder("sess-p5", "", phase="post_generation", language="ko")
    en = suggest_placeholder("sess-p5", "", phase="post_generation", language="en")
    assert "정확한 파일" in ko
    assert "exact file" in en.lower()


def test_suggest_placeholder_defaults_to_korean_when_language_missing():
    ko = suggest_placeholder("sess-p6", "", phase="interview", language=None)
    assert "호텔" in ko  # Korean bundle


def test_suggest_placeholder_rotation_differs_after_recent_history():
    # After a recorded turn, rotation_seed increments
    first = suggest_placeholder("sess-p7", "", phase="interview", language="ko")
    record_modification_request("sess-p7", "something unrelated")
    second = suggest_placeholder("sess-p7", "", phase="interview", language="ko")
    # With 3 rotation options, seeds 0 and 1 give different strings
    assert first != second


# ── Expanded keyword vocabulary (issue #15) ──────────────────────────

@pytest.mark.parametrize(
    "text,expected",
    [
        # Contact flow additions
        ("IVR 흐름 바꿔줘", "contact_flow"),
        ("call flow 수정", "contact_flow"),
        ("콜 라우팅 조정", "contact_flow"),
        ("전화 흐름 다시", "contact_flow"),
        # Prompt additions
        ("system prompt 고쳐", "prompt"),
        ("지시사항 업데이트", "prompt"),
        ("프롬트 수정해줘", "prompt"),  # common typo
        # Infrastructure additions
        ("테이블 이름 변경", "infrastructure"),
        ("GSI 추가해줘", "infrastructure"),
        ("IAM 권한 수정", "infrastructure"),
        ("API Gateway 설정 변경", "infrastructure"),
        ("CDK stack 손봐줘", "infrastructure"),
        # OpenAPI additions
        ("엔드포인트 정리", "openapi"),
        ("REST API 정의 고쳐", "openapi"),
        # Lambda additions
        ("validator 로직 바꿔", "lambda_code"),
        ("핸들러 코드 업데이트", "lambda_code"),
        ("비즈니스 로직 손봐", "lambda_code"),
        # FAQ/KB additions
        ("RAG 검색 소스 교체", "faq"),
        ("지식 소스 추가", "faq"),
        # Session-level additions
        ("종료 멘트 바꿔", "session_flow_config"),
        ("아웃바운드 캠페인 설정", "session_flow_config"),
        ("상담원 연결 흐름", "session_flow_config"),
        ("무응답 대기 시간", "session_flow_config"),
    ],
)
def test_expanded_asset_keywords(text, expected):
    out = normalize_keywords(text)
    assert expected in out, f"{text!r} → {out}, expected {expected}"


def test_tone_ambiguous_bucket():
    # "말투" should resolve to tone_ambiguous (ask: prompt vs persona)
    out = normalize_keywords("말투를 더 친절하게 바꿔줘")
    assert "tone_ambiguous" in out


def test_scenario_ambiguous_bucket():
    out = normalize_keywords("대화 시나리오 수정하고 싶어")
    assert "scenario_ambiguous" in out


def test_ambiguous_keys_constant_exposed():
    from context.modification_tracking import AMBIGUOUS_KEYS
    assert "greeting_ambiguous" in AMBIGUOUS_KEYS
    assert "tone_ambiguous" in AMBIGUOUS_KEYS
    assert "scenario_ambiguous" in AMBIGUOUS_KEYS


def test_state_block_emits_ambiguous_rule():
    # Current turn contains "말투" → tone_ambiguous must trigger the
    # "ASK BEFORE PATCHING" section in the injected state block.
    block = format_modification_state_block("sess-amb", "말투 좀 바꿔")
    assert block is not None
    assert "Ambiguous request" in block
    assert "tone_ambiguous" in block


def test_suggest_placeholder_tone_ambiguous():
    ko = suggest_placeholder("sess-t1", "말투 좀 바꿔", phase="post_generation", language="ko")
    en = suggest_placeholder("sess-t1", "change the tone", phase="post_generation", language="en")
    assert "ai_agent_prompt" in ko and "persona" in ko
    assert "ai_agent_prompt" in en and "persona" in en


def test_suggest_placeholder_scenario_ambiguous():
    ko = suggest_placeholder("sess-s1", "시나리오 수정할래", phase="post_generation", language="ko")
    assert "operation_spec" in ko and "contact_flow" in ko


def test_field_keyword_does_not_swallow_api_field():
    # The operation_spec pattern includes "필드" / "field" but uses a
    # lookbehind to avoid matching "API field" as operation_spec if
    # OpenAPI context is in play. Verify basic behavior — plain "필드"
    # should still route to operation_spec.
    assert "operation_spec" in normalize_keywords("필드 이름 바꿔줘")
