"""Deterministic audit of tool-call claims in an orchestrator turn.

Live (2026-09-14): an orchestrator wrote "`enforce_openapi_contract_tool`
returned: ... `reviewer_agent` returned: {"blocking": []}" and, in another
turn, "generate_acxd_application was actually called ✅" — while the backend's
tool log showed no such calls. The model can narrate any tool result; only
the runtime knows which tools ran. This module compares the two: the turn's
assistant text is scanned for tool names presented as executed, and every
name that has no matching invocation in the same turn is reported so the
user sees the discrepancy where the claim was made.
"""
from __future__ import annotations

import re
from typing import Iterable

# Tools whose results the orchestrator is known to narrate. A name outside
# this set is still checked when it is written with the tool-call verbs below.
_WATCHED = (
    "generate_acxd_application", "reviewer_agent", "enforce_openapi_contract_tool",
    "validate_parameter_consistency", "validate_shape_parity_report", "lint_openapi",
    "lint_contact_flow", "lint_cloudformation", "upsert_acxd_flow_plan",
    "generate_lambda", "generate_openapi", "generate_infrastructure", "generate_contact_flow",
    "generate_faq", "generate_prompt", "run_validation", "package_assets",
)

# "<tool> returned / was called / 호출 / 반환 / 실행" — a claim that the tool ran.
_CLAIM_VERBS = (
    r"(?:returned|was (?:actually )?(?:called|invoked|run|executed)|(?:has been|have been) (?:called|invoked|executed)|"
    r"result(?:s)?(?: JSON| was| were| is| are)|"
    r"호출\s*(?:했|됐|되었|됨|완료|되어|한 결과|결과)|반환|실행\s*(?:했|됐|되었|됨|완료|결과|되어))"
)

# Weaker spellings that also appear in plans and step lists ("1. reviewer_agent
# 호출: 전체 리뷰", "run generate_lambda; its output ..."). They count as a claim
# when a result follows on the same line (a count, a verdict, a JSON block) or
# when nothing in the preceding context reads as an intention.
_WEAK_VERBS = r"(?:호출\s*(?:\)|:|：)|`?\s*[:：](?=\s)|output|result(?:s)?)"

# A tool name followed closely by a result-shaped block is a narrated result
# even without a verb: "`generate_acxd_application` (실제 호출):\n```json {…}".
_RESULT_BLOCK = r"(?:```json|\{\s*\"(?:status|success|blocking|counts)\")"

# What a narrated RESULT looks like on the line after a weak spelling (live:
# "`validate_parameter_consistency`: 불일치 0건, D9 위반 0건" with no tool call).
_RESULT_HINT = re.compile(
    r"(?:\d+\s*건|\b\d+\s*(?:mismatch|violation|problem|finding|error)s?\b|통과|성공|완료|실패|불일치|위반|"
    r"\bSUCCESS\b|\bFAIL(?:ED)?\b|\bPASS(?:ED)?\b|blocking|advisory|problems|✅|❌|```|\{)",
    re.I,
)

# Phrasing that marks an intention rather than an outcome. A strong verb
# ("호출 완료", "returned") is a claim even after such an announcement — the
# live narrations opened with "호출하겠습니다" and then reported a result.
_PLAN_MARKERS = re.compile(
    r"(?:\bwill\b|\bwould\b|\bgoing to\b|\bplan(?:ning)? to\b|\bnext step|\blet me\b|\bI'?ll\b|\bshall\b|"
    r"\bonce you\b|\bif you\b|예정|하겠|할게|할 것|하려|드릴게|드리겠|다음 단계|진행할|승인|해 ?주시면|주시면)",
    re.I,
)
_PLAN_CONTEXT = 300

# An execution claim that names no tool at all ("ACXD 재생성과 정합성 검증을 모두
# 실제로 실행했습니다", "I ran the validation") — only the runtime knows whether
# ANY tool ran this turn, so a turn with zero tool calls and such a sentence is
# reported without a name.
_EXECUTED_CLAIM = re.compile(
    r"(?:실제로\s*(?:실행|호출|수행)(?:했|됐|되었|완료)|(?:실행|호출|수행)(?:했습니다|했어요|을 완료|이 완료|했고)|"
    r"(?:재생성|재검증|검증)(?:을|를)?\s*(?:모두\s*)?(?:실제로\s*)?(?:실행|완료)(?:했|됐)|"
    # "ACXD 재생성 완료." / "검증 완료" — a bare completion, no verb ending at all
    r"(?:재생성|재검증|검증|생성|배포|호출|실행|패키징)\s*완료|"
    # "도구 반환값 그대로 전달합니다" / "결과를 그대로 보고합니다" — presenting output as a tool's
    r"(?:도구|툴|tool)\s*(?:의\s*)?(?:반환값|반환 값|결과|출력|응답)(?:을|를|은|는)?\s*(?:그대로\s*)?(?:전달|보고|공유|정리)|"
    r"(?:반환값|반환 값)(?:을|를|은|는)?\s*그대로|"
    r"\b(?:I|we)\s+(?:actually\s+)?(?:ran|executed|invoked|called)\b|\b(?:was|were|has been|have been)\s+(?:actually\s+)?(?:executed|run|invoked|called)\b|"
    r"\bexecuted successfully\b|\b(?:the\s+)?tool(?:'s)?\s+(?:returned|output|result)s?\b|\breturned values?\b)",
    re.I,
)

# The keys our generation/validation tools answer with. Two or more of them
# with numbers, in a turn that called no tool, is a fabricated tool result even
# when no verb claims anything (live: "- flows: 9 / slot_types: 2 / problems: 0
# / mismatches: 0" after a turn with zero tool calls).
_TOOL_OUTPUT_KEY = re.compile(
    r"\b(?:flows?|slot_types?|data_requests?|problems?|mismatches|blocking|advisory|violations?|findings?)\b"
    r"\**\s*[:：]\s*\**\s*\d+",
    re.I,
)
_MIN_OUTPUT_KEYS = 2


def _strong_claim(pattern: str, text: str) -> bool:
    """A match that is not itself phrased as a plan."""
    return any(not _PLAN_MARKERS.search(m.group(0)) for m in re.finditer(pattern, text, re.I))


def _weak_claim(pattern: str, text: str) -> bool:
    """A weak spelling is a claim when a result follows it on the same line;
    otherwise only when the preceding context carries no intention."""
    for m in re.finditer(pattern, text, re.I):
        rest_of_line = text[m.end():text.find("\n", m.end()) if text.find("\n", m.end()) != -1 else len(text)]
        if _RESULT_HINT.search(rest_of_line[:200]):
            return True
        if not _PLAN_MARKERS.search(text[max(0, m.start() - _PLAN_CONTEXT):m.end()]):
            return True
    return False


def _name_verb_pattern(name: str, verbs: str) -> str:
    # `name` ... verb within ~80 chars, or verb ... `name` (Korean puts the verb last).
    # A name written as a JSON key — "generate_lambda": true — is data the model is
    # quoting (live 2026-09-20: a session tool's flags inside a ```json block), not a
    # narrated call, so a double-quoted spelling never matches.
    quoted = rf'(?<!")`?{re.escape(name)}`?(?!")'
    return rf"{quoted}[^\n`]{{0,80}}?{verbs}|{verbs}[^\n`]{{0,40}}?{quoted}"


def _claimed_tools(text: str) -> set[str]:
    claimed: set[str] = set()
    if not text:
        return claimed
    for name in _WATCHED:
        if _strong_claim(_name_verb_pattern(name, _CLAIM_VERBS), text):
            claimed.add(name)
            continue
        if _weak_claim(_name_verb_pattern(name, _WEAK_VERBS), text):
            claimed.add(name)
            continue
        block = rf'(?<!")`?{re.escape(name)}`?(?!")[^`]{{0,120}}?{_RESULT_BLOCK}'
        if re.search(block, text, re.I | re.S):
            claimed.add(name)
    return claimed


def unbacked_tool_claims(text: str, tools_called: Iterable[str]) -> list[str]:
    """Tool names the text presents as executed that were not called this turn."""
    called = {str(t) for t in tools_called if t}
    return sorted(name for name in _claimed_tools(text) if name not in called)


def unbacked_execution_claim(text: str, tools_called: Iterable[str]) -> bool:
    """True when the turn called NO tool yet the text asserts something was executed
    — by a verb, by presenting "the tool's return value", or by listing two or more
    of the tools' own output keys with numbers."""
    if any(str(t) for t in tools_called if t) or not text:
        return False
    if any(not _PLAN_MARKERS.search(m.group(0)) for m in _EXECUTED_CLAIM.finditer(text)):
        return True
    keys = {m.group(0).split(":")[0].split("：")[0].strip("* ").lower() for m in _TOOL_OUTPUT_KEY.finditer(text)}
    return len(keys) >= _MIN_OUTPUT_KEYS


def audit_notice(text: str, tools_called: Iterable[str], language: str = "ko") -> str | None:
    """A user-facing notice for unbacked claims, or None when the turn is clean."""
    missing = unbacked_tool_claims(text, tools_called)
    if not missing:
        if unbacked_execution_claim(text, tools_called):
            if str(language or "ko").lower().startswith("ko"):
                return ("\n\n⚠️ 검증 안내: 이 턴은 도구를 실행했다고 말하지만 서버 기록에는 어떤 도구 호출도 없습니다. "
                        "위 결과는 실제 실행 결과가 아닐 수 있으니, 해당 도구를 다시 실행해 확인해 주세요.")
            return ("\n\n⚠️ Verification notice: this turn says something was executed, but the server "
                    "recorded no tool call at all. Treat that output as unverified and re-run the tool.")
        return None
    names = ", ".join(f"`{m}`" for m in missing)
    if str(language or "ko").lower().startswith("ko"):
        return (f"\n\n⚠️ 검증 안내: 이 턴에서 {names} 호출/결과가 언급되었지만 서버 기록에는 해당 도구 호출이 없습니다. "
                f"위 결과는 실제 실행 결과가 아닐 수 있으니, 해당 도구를 다시 실행해 확인해 주세요.")
    return (f"\n\n⚠️ Verification notice: this turn mentions {names} as called or returning a result, "
            f"but the server recorded no such tool call. Treat that output as unverified and re-run the tool.")
