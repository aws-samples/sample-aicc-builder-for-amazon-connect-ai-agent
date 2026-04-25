"""
Modification Tracking — Per-session log of user modification requests.

Purpose:
1. Map user-typed keywords ("flow", "프롬프트", "인프라" ...) to the canonical
   asset key, so the orchestrator knows which file the user is really referring
   to rather than guessing.
2. Count repeated corrections on the same asset. If the user says the same
   thing twice and the previous edit claimed success, the orchestrator should
   STOP patching and ask for disambiguation instead of guessing again.

The state is persisted to NFS (mirrors context/generation_progress.py) so it
survives WS reconnects and compaction. app.py injects a <modification_state>
XML block into the user turn, same shape as <generation_state>.

NFS path: /mnt/s3/sessions/{session_id}/context/modification_state.json
"""

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Keyword → canonical asset key ─────────────────────────────────────
# Regex patterns (case-insensitive); first match wins. Order matters for
# ambiguous phrases — more specific phrases must come first.
#
# Canonical asset keys:
#   contact_flow, prompt, infrastructure, openapi, lambda_code, faq,
#   operation_spec, session_flow_config, greeting_ambiguous
# Use ASCII-only word boundary (?:(?<![A-Za-z])word(?![A-Za-z])) so English tokens
# are matched even when followed by Korean characters (Python's default \b
# considers Hangul "word" characters, which breaks `\bflow\b` against "flow에서").
def _ascii_word(w: str) -> str:
    return rf"(?:(?<![A-Za-z]){w}(?![A-Za-z]))"


ASSET_KEYWORDS: List[Tuple[str, str]] = [
    # ── Ambiguous buckets first — they need disambiguation, not a guess ──
    # Greeting: contact_flow lex-bot Text vs prompt opening line
    (
        r"(첫\s*멘트|인사\s*말|greeting|welcome\s*message|오프닝\s*멘트|오프닝\s*프롬프트|시작\s*멘트)",
        "greeting_ambiguous",
    ),
    # Tone / speaking style: prompt (AI agent's conversational tone) vs
    # session_flow_config.persona (session-level persona)
    (
        r"(말투|어투|톤|tone|말씨|speaking\s*style|voice\s*tone)",
        "tone_ambiguous",
    ),
    # Scenario / dialog flow: operation_spec.conversation_steps vs
    # prompt.conversation_script vs contact_flow block sequence
    (
        r"(시나리오|대화\s*시나리오|대화\s*흐름|conversation\s*flow|dialog\s*flow|dialogue\s*flow)",
        "scenario_ambiguous",
    ),

    # ── Contact flow ───────────────────────────────────────────────────
    (
        r"(contact\s*flow|call\s*flow|컨택트\s*플로우|컨택\s*플로우|콜\s*플로우|"
        r"플로우|전화\s*흐름|통화\s*흐름|콜\s*라우팅|ivr)",
        "contact_flow",
    ),
    (_ascii_word("flow"), "contact_flow"),

    # ── AI agent prompt ────────────────────────────────────────────────
    (
        r"(ai\s*agent\s*prompt|에이전트\s*프롬프트|봇\s*프롬프트|"
        r"시스템\s*프롬프트|system\s*prompt|instruction|지시\s*사항|프롬프트|프롬트)",
        "prompt",
    ),
    (_ascii_word("prompt"), "prompt"),

    # ── Infrastructure / CloudFormation ─────────────────────────────────
    (
        r"(cloud\s*formation|cfn|cdk|인프라|infrastructure|dynamodb|"
        r"테이블|table|데이터베이스|스키마|schema|gsi|인덱스|"
        r"iam|권한|정책|api\s*gateway|게이트웨이)",
        "infrastructure",
    ),

    # ── OpenAPI ────────────────────────────────────────────────────────
    (
        r"(open\s*api|openapi|api\s*스펙|api\s*spec|api\s*정의|"
        r"엔드\s*포인트|endpoint|rest\s*api)",
        "openapi",
    ),

    # ── Lambda code ────────────────────────────────────────────────────
    (
        r"(람다|lambda|lambda\s*코드|lambda\s*function|핸들러|handler|"
        r"validator|검증\s*로직|비즈니스\s*로직|business\s*logic)",
        "lambda_code",
    ),

    # ── FAQ / Knowledge ────────────────────────────────────────────────
    (
        r"(faq|지식\s*베이스|knowledge\s*base|kb|지식\s*소스|rag|검색\s*소스|knowledge\s*source)",
        "faq",
    ),

    # ── Spec / requirements ────────────────────────────────────────────
    (
        r"(요구\s*사항|스펙|requirement|spec|operation\s*spec|"
        r"비즈니스\s*룰|business\s*rule|입력\s*값|출력\s*값|input\s*field|output\s*field|"
        r"(?<!API\s)필드|(?<!API\s)field)",
        "operation_spec",
    ),

    # ── Session-level config ───────────────────────────────────────────
    (
        r"(세션\s*설정|session\s*config|녹음|recording|페르소나|persona|"
        r"종료\s*멘트|마무리\s*인사|closing|아웃바운드|outbound|캠페인|campaign|"
        r"상담원\s*연결|agent\s*transfer|무응답|no[-\s]?response|대기\s*시간|timeout)",
        "session_flow_config",
    ),
]

# Canonical keys that the user cannot resolve on their own — the
# orchestrator MUST ask for disambiguation instead of picking a default.
AMBIGUOUS_KEYS = {"greeting_ambiguous", "tone_ambiguous", "scenario_ambiguous"}

# Human-readable labels (mixed ko/en) for prompt injection
ASSET_LABELS: Dict[str, str] = {
    "contact_flow": "Contact Flow JSON (assets/contact_flow/contact_flow.json)",
    "prompt": "AI Agent Prompt (assets/prompts/ai_agent_prompt.yaml)",
    "infrastructure": "CloudFormation Infrastructure (assets/cloudformation/infrastructure.yaml)",
    "openapi": "OpenAPI Spec (assets/openapi/openapi.yaml)",
    "lambda_code": "Lambda function code (assets/lambda/{operation_id}/index.py)",
    "faq": "FAQ / Knowledge Base documents (assets/knowledge-base/)",
    "operation_spec": "Operation Spec (update via update_operation_spec)",
    "session_flow_config": "Session Flow Config (update via save_session_flow_config)",
    "greeting_ambiguous": (
        "AMBIGUOUS — could be contact_flow (lex-bot Text), "
        "prompt opening line, or session_flow_config.common_greeting"
    ),
    "tone_ambiguous": (
        "AMBIGUOUS — could be prompt (AI agent's conversational tone) "
        "OR session_flow_config.agent_persona"
    ),
    "scenario_ambiguous": (
        "AMBIGUOUS — could be operation_spec.conversation_steps, "
        "prompt.conversation_script, or contact_flow block sequence"
    ),
}

# Ring-buffer cap for recent_requests
MAX_RECENT_REQUESTS = 8


def _state_path(session_id: str) -> Path:
    mount_path = os.environ.get("S3FILES_MOUNT_PATH", "/mnt/s3")
    safe_id = session_id.replace("..", "_").replace("/", "_")
    return Path(mount_path) / "sessions" / safe_id / "context" / "modification_state.json"


def _read_state(session_id: str) -> Dict[str, Any]:
    path = _state_path(session_id)
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"[modification_tracking] read failed: {e}")
    return {"recent_requests": [], "repeat_counter": {}}


def _write_state(session_id: str, state: Dict[str, Any]) -> None:
    path = _state_path(session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, default=str)
        tmp.rename(path)
    except Exception as e:
        logger.warning(f"[modification_tracking] write failed: {e}")


def normalize_keywords(user_text: str) -> List[str]:
    """Extract canonical asset keys from raw user text.

    Returns a de-duplicated list preserving first-match order. Empty list
    means no asset keyword matched — the message is probably not a targeted
    modification request.
    """
    if not user_text:
        return []
    found: List[str] = []
    text = user_text
    for pattern, key in ASSET_KEYWORDS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            if key not in found:
                found.append(key)
    return found


def record_modification_request(
    session_id: str,
    user_text: str,
    keywords: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Record a user modification request and bump repeat counters.

    Call BEFORE the orchestrator acts, so the counter reflects this turn.
    Returns the updated state so callers (app.py) can render it inline.
    """
    if keywords is None:
        keywords = normalize_keywords(user_text)

    state = _read_state(session_id)
    recent: List[Dict[str, Any]] = state.setdefault("recent_requests", [])
    counter: Dict[str, int] = state.setdefault("repeat_counter", {})

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    entry = {
        "ts": now,
        "user_text": (user_text or "")[:400],
        "keywords": keywords,
        "outcome": None,  # filled in by record_modification_outcome
    }
    recent.append(entry)
    # Ring buffer
    if len(recent) > MAX_RECENT_REQUESTS:
        state["recent_requests"] = recent[-MAX_RECENT_REQUESTS:]

    # Bump counter per-keyword
    for k in keywords:
        counter[k] = counter.get(k, 0) + 1

    _write_state(session_id, state)
    logger.info(
        f"[modification_tracking] recorded request session={session_id} "
        f"keywords={keywords} counters={ {k: counter[k] for k in keywords} }"
    )
    return state


def record_modification_outcome(
    session_id: str,
    outcome: str,
    target_asset: Optional[str] = None,
) -> None:
    """Attach an outcome to the most recent request entry.

    outcome values: "claimed_success", "escalated_spec_level", "disambiguation_asked",
                    "error", "skipped".
    """
    state = _read_state(session_id)
    recent = state.get("recent_requests") or []
    if not recent:
        logger.debug(f"[modification_tracking] no recent request to annotate for {session_id}")
        return
    recent[-1]["outcome"] = outcome
    if target_asset:
        recent[-1]["target_asset"] = target_asset
    _write_state(session_id, state)


def reset_repeat_counter(session_id: str, keyword: str) -> None:
    """Zero the counter for a keyword after the user disambiguates successfully."""
    state = _read_state(session_id)
    counter = state.setdefault("repeat_counter", {})
    if keyword in counter:
        counter[keyword] = 0
        _write_state(session_id, state)


def format_modification_state_block(session_id: str, current_user_text: str) -> Optional[str]:
    """Produce the <modification_state> body for prompt injection.

    Returns None when there is nothing useful to inject (no keywords in the
    current turn AND no counters > 0). Otherwise returns a plain-text body
    that app.py will wrap in <modification_state>…</modification_state>.
    """
    current_keywords = normalize_keywords(current_user_text)
    state = _read_state(session_id)
    recent = state.get("recent_requests") or []
    counter: Dict[str, int] = state.get("repeat_counter") or {}

    # If this turn has no keywords AND no prior activity, skip injection
    if not current_keywords and not any(v > 0 for v in counter.values()):
        return None

    lines: List[str] = []
    lines.append(
        "Below is the modification context for this session. Use it to route "
        "edits to the RIGHT asset and to detect repeated corrections."
    )
    lines.append("")

    # Current-turn parse
    if current_keywords:
        lines.append("## Current turn — keyword parse")
        for k in current_keywords:
            label = ASSET_LABELS.get(k, k)
            lines.append(f"- `{k}` → {label}")
        lines.append("")

    # If ANY ambiguous bucket fired on this turn, force disambiguation
    ambiguous_hits = [k for k in current_keywords if k in AMBIGUOUS_KEYS]
    if ambiguous_hits:
        lines.append("## ⚠️ Ambiguous request — ASK BEFORE PATCHING")
        lines.append(
            "The user's wording maps to multiple possible assets. Do NOT "
            "guess. List the candidate files (see labels above) and ask "
            "which one to edit. Candidates per bucket:"
        )
        lines.append(
            "- `greeting_ambiguous` → (a) contact_flow lex-bot Text, "
            "(b) ai_agent_prompt.yaml opening line, "
            "(c) session_flow_config.common_greeting"
        )
        lines.append(
            "- `tone_ambiguous` → (a) ai_agent_prompt.yaml conversational "
            "tone, (b) session_flow_config.agent_persona"
        )
        lines.append(
            "- `scenario_ambiguous` → (a) operation_spec.conversation_steps "
            "(spec-level), (b) ai_agent_prompt.yaml conversation_script, "
            "(c) contact_flow block sequence"
        )
        lines.append("")

    # Repeat counters — show entries > 1
    hot = {k: v for k, v in counter.items() if v >= 2}
    if hot:
        lines.append("## Repeat counters (possible disambiguation needed)")
        for k, v in hot.items():
            label = ASSET_LABELS.get(k, k)
            lines.append(f"- `{k}` requested {v}× → {label}")
        lines.append(
            "⛔ RULE: If the same keyword has been requested ≥ 2 times AND the "
            "most recent outcome was `claimed_success`, DO NOT patch again. "
            "Ask the user to pick the exact file (show the paths above) before "
            "acting. Then call the orchestrator tool that resets the counter "
            "implicitly by targeting the confirmed asset."
        )
        lines.append("")

    # Recent history (last 3)
    if recent:
        lines.append("## Recent requests (latest last)")
        for entry in recent[-3:]:
            ts = entry.get("ts", "")
            kws = ",".join(entry.get("keywords") or []) or "-"
            out = entry.get("outcome") or "pending"
            txt = (entry.get("user_text") or "")[:80]
            lines.append(f"- [{ts}] keywords=[{kws}] outcome={out} :: {txt!r}")
        lines.append("")

    # Hard rule reminder
    lines.append(
        "## Asset vocabulary reminder\n"
        "- 'flow' / '플로우' / 'IVR' / '콜 라우팅' → Contact Flow JSON (NOT the AI prompt)\n"
        "- '프롬프트' / 'system prompt' / '지시사항' → AI Agent Prompt YAML\n"
        "- '테이블' / 'GSI' / 'IAM' / 'API Gateway' → infrastructure (CFN)\n"
        "- '엔드포인트' / 'API 스펙' / 'OpenAPI' → openapi spec\n"
        "- '핸들러' / 'validator' / '비즈니스 로직' → lambda_code\n"
        "- '녹음' / 'recording' / '아웃바운드' / '종료 멘트' / '상담원 연결' → session_flow_config (spec-level)\n"
        "- '슬롯 단위' / '운영 시간' / '데이터 모델' / '비즈니스 룰' / '필드' → operation_spec (spec-level)\n"
        "- AMBIGUOUS (ask which file the user means):\n"
        "  · '첫 멘트' / '인사말' / 'greeting' → greeting_ambiguous\n"
        "  · '말투' / '톤' / 'tone' → tone_ambiguous (prompt vs persona)\n"
        "  · '시나리오' / '대화 흐름' → scenario_ambiguous (spec vs prompt vs contact_flow)"
    )

    return "\n".join(lines)


# ── Phase-aware chat placeholder suggestion ──────────────────────────
# Selected per turn by suggest_placeholder() and pushed to the frontend
# via an `input_hint` WebSocket event. Copy is intentionally short —
# it has to fit inside a single textarea placeholder line on a mobile
# viewport. Keep under ~80 chars in each language.
#
# Rotation note: indices that iterate over a counter use len(recent)
# as the rotation seed so the same suggestion doesn't lock in; no
# client-side state needed.

_PLACEHOLDERS = {
    "ko": {
        "fallback": "메시지를 입력하세요",
        "interview": [
            "예: '호텔 예약 AI 상담원 만들고 싶어요'",
            "예: 'ABC 호텔이고 예약 조회·취소가 주 업무예요'",
            "예: '보험사인데 청구 상태 조회가 많아요'",
        ],
        "generation": (
            "진행: '네, 계속 진행해주세요' / 멈춤: '잠깐, ~를 바꿔주세요'"
        ),
        "post_generation": [
            "수정 예: 'contact_flow의 첫 멘트를 \"안녕하세요...\"로 변경'",
            "수정 예: 'ai_agent_prompt.yaml의 톤을 더 격식 있게'",
            "수정 예: '녹음 기능 꺼줘 (session_flow_config)'",
        ],
        "ambiguous": {
            "greeting_ambiguous": (
                "'첫 멘트'가 (a) contact_flow 인사말 (b) ai_agent_prompt 시작 문구 (c) session common_greeting 중 어디?"
            ),
            "tone_ambiguous": (
                "'말투/톤'이 (a) ai_agent_prompt 대화 톤 (b) session_flow_config persona 중 어디?"
            ),
            "scenario_ambiguous": (
                "'시나리오'가 (a) operation_spec 스펙 (b) ai_agent_prompt 대본 (c) contact_flow 블록 중 어디?"
            ),
        },
        "repeat": (
            "정확한 파일을 알려주세요 — 예: 'contact_flow.json 인사말 변경' 또는 'ai_agent_prompt.yaml 톤 변경'"
        ),
    },
    "en": {
        "fallback": "Type a message",
        "interview": [
            "e.g. 'I want a hotel-booking AI agent'",
            "e.g. 'ABC Hotel; mainly reservation lookup + cancel'",
            "e.g. 'Insurance company — lots of claim-status calls'",
        ],
        "generation": (
            "Continue: 'yes, proceed' / Pause: 'wait, change ~ first'"
        ),
        "post_generation": [
            "Edit: 'change contact_flow greeting to \"Hello...\"'",
            "Edit: 'make ai_agent_prompt.yaml tone more formal'",
            "Edit: 'turn recording off (session_flow_config)'",
        ],
        "ambiguous": {
            "greeting_ambiguous": (
                "Is the greeting (a) contact_flow lex Text, (b) ai_agent_prompt opener, or (c) session common_greeting?"
            ),
            "tone_ambiguous": (
                "Is 'tone' (a) ai_agent_prompt conversational tone or (b) session_flow_config persona?"
            ),
            "scenario_ambiguous": (
                "Is 'scenario' (a) operation_spec, (b) ai_agent_prompt script, or (c) contact_flow blocks?"
            ),
        },
        "repeat": (
            "Name the exact file — e.g. 'contact_flow.json greeting' or 'ai_agent_prompt.yaml tone'"
        ),
    },
}


def _pick_language(language: Optional[str]) -> str:
    if not language:
        return "ko"
    lang = language.lower()
    if lang.startswith("ko"):
        return "ko"
    return "en"


def suggest_placeholder(
    session_id: str,
    current_user_text: str,
    phase: Optional[str] = None,
    language: Optional[str] = None,
) -> str:
    """Return the chat textarea placeholder the frontend should show next.

    Priority: repeat-correction > ambiguous-bucket (greeting/tone/scenario)
    > phase-specific > fallback. Safe to call on every turn — reads the
    per-session state from NFS and returns a short string suitable for
    ::placeholder.
    """
    lang = _pick_language(language)
    bundle = _PLACEHOLDERS[lang]

    try:
        state = _read_state(session_id)
    except Exception:
        state = {"recent_requests": [], "repeat_counter": {}}

    recent = state.get("recent_requests") or []
    counter: Dict[str, int] = state.get("repeat_counter") or {}
    current_keywords = normalize_keywords(current_user_text or "")

    # 1) Repeat-correction: same keyword ≥2 with a claimed_success outcome
    last_outcome = (recent[-1].get("outcome") if recent else None) or ""
    hot_keys = [k for k, v in counter.items() if v >= 2]
    if hot_keys and last_outcome == "claimed_success":
        return bundle["repeat"]

    # 2) Any ambiguous bucket in this very turn — pick the first hit so the
    # hint stays targeted to what the user actually said.
    for k in current_keywords:
        if k in AMBIGUOUS_KEYS:
            amb = bundle.get("ambiguous", {})
            if k in amb:
                return amb[k]

    # 3) Phase-specific suggestions
    rotation_seed = len(recent)
    if phase == "post_generation":
        opts = bundle["post_generation"]
        return opts[rotation_seed % len(opts)]
    if phase == "generation":
        return bundle["generation"]
    if phase == "interview":
        opts = bundle["interview"]
        return opts[rotation_seed % len(opts)]

    return bundle["fallback"]


__all__ = [
    "ASSET_KEYWORDS",
    "ASSET_LABELS",
    "AMBIGUOUS_KEYS",
    "normalize_keywords",
    "record_modification_request",
    "record_modification_outcome",
    "reset_repeat_counter",
    "format_modification_state_block",
    "suggest_placeholder",
]
