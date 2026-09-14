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
    r"result(?:s)?(?: JSON)?(?: was| were| is| are)?|output|"
    r"호출(?:했|됐|되었|됨|완료|되어|한 결과)|반환|실행(?:했|됐|되었|됨|완료|결과|되어))"
)


def _claimed_tools(text: str) -> set[str]:
    claimed: set[str] = set()
    if not text:
        return claimed
    for name in _WATCHED:
        # `name` ... verb within ~80 chars, or verb ... `name` (Korean puts the verb last)
        pattern = rf"`?{re.escape(name)}`?[^\n`]{{0,80}}?{_CLAIM_VERBS}|{_CLAIM_VERBS}[^\n`]{{0,40}}?`?{re.escape(name)}`?"
        if re.search(pattern, text, re.I):
            claimed.add(name)
    return claimed


def unbacked_tool_claims(text: str, tools_called: Iterable[str]) -> list[str]:
    """Tool names the text presents as executed that were not called this turn."""
    called = {str(t) for t in tools_called if t}
    return sorted(name for name in _claimed_tools(text) if name not in called)


def audit_notice(text: str, tools_called: Iterable[str], language: str = "ko") -> str | None:
    """A user-facing notice for unbacked claims, or None when the turn is clean."""
    missing = unbacked_tool_claims(text, tools_called)
    if not missing:
        return None
    names = ", ".join(f"`{m}`" for m in missing)
    if str(language or "ko").lower().startswith("ko"):
        return (f"\n\n⚠️ 검증 안내: 이 턴에서 {names} 호출/결과가 언급되었지만 서버 기록에는 해당 도구 호출이 없습니다. "
                f"위 결과는 실제 실행 결과가 아닐 수 있으니, 해당 도구를 다시 실행해 확인해 주세요.")
    return (f"\n\n⚠️ Verification notice: this turn mentions {names} as called or returning a result, "
            f"but the server recorded no such tool call. Treat that output as unverified and re-run the tool.")
