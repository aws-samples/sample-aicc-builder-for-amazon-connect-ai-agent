"""Deterministic backstop for a turn that reports results it never produced.

Live (dev, 2026-09-26, ACXD only): the user asked to fix an enum typo and
package; the orchestrator answered "재검증: 차단 항목 0건 … 종합세추 잔재 0건
(전체 워크스페이스 검색으로 확인)" in a turn that called NO tool — S3 still held
the typo. The prompt's tool-honesty rules did not prevent it, and the chat gave
the user no way to tell. The backend knows how many tools the turn ran, so a
reply that claims fixes/verification after a zero-tool turn gets a plain notice.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# Phases in which the orchestrator changes or checks assets. The interview asks
# and answers; a zero-tool turn there is ordinary.
_WORK_PHASES = ("generation", "post_generation", "review")

# Words a reply uses to say work happened. A recap of an EARLIER turn matches
# too; the notice below is worded as a fact about THIS turn, so it stays true.
_CLAIM = re.compile(
    r"(완료|반영(했|됐|되었|완료)|수정(했|됐|되었|완료)|재생성(했|됐|되었|완료)?|교정(했|됐|되었|완료)"
    r"|검증(했|됐|되었|결과|완료)|확인(했|됐|되었)|해소|0\s*건"
    r"|\b(fixed|updated|regenerated|re-?generated|verified|validated|packaged|corrected)\b"
    r"|\b0\s+(violations?|mismatches|issues|findings)\b"
    r"|修正しました|再生成しました|確認しました|完了しました)",
    re.IGNORECASE,
)

_NOTICE = {
    "ko": ("이번 응답에서는 도구가 한 번도 실행되지 않았습니다. 응답에 적힌 수정·재생성·검증 결과는 "
           "이번 턴에 실제로 수행된 것이 아닙니다. 반영이 필요하면 다시 요청해 주세요."),
    "en": ("No tool ran during this reply. The fixes, regenerations or checks it describes were not "
           "performed in this turn. Ask again if they still need to be made."),
    "ja": ("この応答ではツールが一度も実行されていません。記載された修正・再生成・検証は、このターンでは"
           "実際には行われていません。必要であれば、もう一度依頼してください。"),
}


def count_tool_uses(messages: List[Dict[str, Any]]) -> int:
    """toolUse blocks in the turn's assistant messages (Strands format)."""
    count = 0
    for msg in messages or []:
        if msg.get("role") != "assistant":
            continue
        for block in msg.get("content") or []:
            if isinstance(block, dict) and "toolUse" in block:
                count += 1
    return count


def reply_text(messages: List[Dict[str, Any]]) -> str:
    """All assistant text of the turn."""
    parts: List[str] = []
    for msg in messages or []:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
            continue
        for block in content or []:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
    return "\n".join(parts)


def unbacked_claim_notice(messages: List[Dict[str, Any]], phase: Optional[str],
                          language: Optional[str] = "ko-KR", *,
                          tools_started: int = 0) -> Optional[str]:
    """The notice to show when a work-phase turn claims results but ran no tool.

    ``tools_started`` is the number of tool invocations the stream itself saw.
    The turn's message slice alone is not enough: live (Hanbit, 2026-09-27) the
    agent's history was cut back mid-turn (9 messages, pre-count 8), the slice
    held only the closing text, and a turn that had generated the whole FAQ got
    "no tool ran" in the chat."""
    if (phase or "") not in _WORK_PHASES:
        return None
    if not messages or tools_started > 0 or count_tool_uses(messages) > 0:
        return None
    if not _CLAIM.search(reply_text(messages)):
        return None
    code = str(language or "ko").lower()[:2]
    return _NOTICE.get(code, _NOTICE["en"])
