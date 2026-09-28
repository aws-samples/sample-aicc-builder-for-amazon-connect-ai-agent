"""Requirement items — the document the customer handed over, kept item by item
until every item is accounted for in a spec.

Why: when a requirements document arrives, the orchestrator keeps a short
summary in context and writes the specs from that. Live (2026-09-20): a
document that said `include_customer_phone_lookup=true`, listed seven FAQ
topics under `## FAQ` and named "3회 실패" as an escalation rule produced a
contact flow with the lookup off, no knowledge base and no FAQ asset — the
review, which compares assets with the PLAN, found nothing wrong. The summary
step is where the document was lost, so the document is now split into items
the moment it is saved, each item has to be MAPPED to what it became (an
operation, a field, a flow, the KB, the session or contact-flow config) or
EXCLUDED with the customer's reason, and `complete_interview` refuses while
items are still unaccounted for. A few statements that have exactly one
correct spec expression are checked literally as well.

Storage: `requirement_items.json` in the session workspace (NFS + S3), next to
`flow_config.json`.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Optional, Union

from strands import tool

logger = logging.getLogger(__name__)

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_UNDERLINE_HEADING = re.compile(r"^\s*(=+|-{3,})\s*$")
_BULLET = re.compile(r"^(\s*)(?:[-*•·▪◦]|\d{1,3}[.)]|[가-힣][.)]|\(\d{1,3}\))\s+(.+)$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_MIN_ITEM_CHARS = 4

TARGET_KINDS = (
    "operation", "field", "flow", "kb", "guardrail", "session_config", "contact_flow",
    "infrastructure", "persona", "excluded",
)

# ── statements with exactly one correct spec expression ─────────────────────
_PHONE_LOOKUP = re.compile(r"include_customer_phone_lookup\s*[=:：]\s*(true|false)", re.I)
_FAQ_HEADING = re.compile(r"\bFAQ\b|자주\s*묻는|지식\s*베이스|knowledge\s*base|よくある質問", re.I)
_IDENTITY = re.compile(
    r"본인\s*확인|본인\s*인증|신원\s*확인|identity\s*verification|verify\s+(?:the\s+)?(?:caller|customer|identity)"
    r"|authenticat|本人確認", re.I)
_MAX_FAILURES = re.compile(
    r"(\d)\s*회\s*(?:연속\s*)?(?:실패|오류|오답|미인식|불일치)|(?:after\s+)?(\d)\s*(?:failed|unsuccessful|invalid|wrong)\s+"
    r"(?:attempts?|tries|answers?)|(\d)\s*回\s*(?:失敗|エラー)", re.I)


def split_requirement_items(text: str) -> list[dict]:
    """Deterministic split of a requirements document into items.

    Headings (`#`, or a line underlined with === / ---) name the section; every
    bullet / numbered line is an item (deeper-indented continuation lines are
    appended to it); a run of plain lines is one paragraph item; a run of table
    rows is one table item. Ids are R1..Rn in document order."""
    items: list[dict] = []
    section = ""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    para: list[str] = []
    table: list[str] = []
    bullet: Optional[dict] = None
    bullet_indent = 0

    def flush_para() -> None:
        nonlocal para
        if para:
            body = " ".join(s.strip() for s in para).strip()
            if len(body) >= _MIN_ITEM_CHARS:
                items.append({"id": f"R{len(items) + 1}", "section": section, "kind": "paragraph", "text": body})
            para = []

    def flush_table() -> None:
        nonlocal table
        if table:
            body = "\n".join(table)
            items.append({"id": f"R{len(items) + 1}", "section": section, "kind": "table", "text": body})
            table = []

    def flush_bullet() -> None:
        nonlocal bullet
        if bullet is not None:
            if len(bullet["text"]) >= _MIN_ITEM_CHARS:
                bullet["id"] = f"R{len(items) + 1}"
                items.append(bullet)
            bullet = None

    for idx, raw in enumerate(lines):
        line = raw.rstrip()
        if not line.strip():
            flush_para(); flush_table(); flush_bullet()
            continue
        m = _HEADING.match(line)
        if m:
            flush_para(); flush_table(); flush_bullet()
            section = m.group(2).strip()
            continue
        if _UNDERLINE_HEADING.match(line) and para and len(para) == 1 and not table:
            section = para[0].strip()          # "Title\n=====" style heading
            para = []
            continue
        if _TABLE_ROW.match(line):
            flush_para(); flush_bullet()
            if not re.match(r"^\s*\|[\s:|-]+\|\s*$", line):   # skip the |---|---| separator
                table.append(line.strip())
            continue
        flush_table()
        b = _BULLET.match(line)
        if b:
            flush_para(); flush_bullet()
            bullet = {"section": section, "kind": "bullet", "text": b.group(2).strip()}
            bullet_indent = len(b.group(1))
            continue
        indent = len(line) - len(line.lstrip())
        if bullet is not None and indent > bullet_indent:
            bullet["text"] = f"{bullet['text']} {line.strip()}"      # continuation of the bullet
            continue
        flush_bullet()
        para.append(line)
    flush_para(); flush_table(); flush_bullet()
    return items


def requirement_signals(items: Iterable[dict]) -> dict:
    """Statements in the document that have exactly one correct spec expression."""
    signals: dict[str, Any] = {}
    faq_items: list[str] = []
    identity_items: list[str] = []
    for item in items:
        text = str(item.get("text") or "")
        section = str(item.get("section") or "")
        m = _PHONE_LOOKUP.search(text)
        if m and "phone_lookup" not in signals:
            signals["phone_lookup"] = {"value": m.group(1).lower() == "true", "item_id": item.get("id")}
        if _FAQ_HEADING.search(section) or _FAQ_HEADING.match(text):
            faq_items.append(str(item.get("id")))
        if _IDENTITY.search(text):
            identity_items.append(str(item.get("id")))
        m = _MAX_FAILURES.search(text)
        if m and "max_failures" not in signals:
            n = next(g for g in m.groups() if g)
            signals["max_failures"] = {"value": int(n), "item_id": item.get("id")}
    if faq_items:
        signals["faq"] = {"item_ids": faq_items}
    if identity_items:
        signals["identity_verification"] = {"item_ids": identity_items}
    quotes = quoted_sentences(items)
    if quotes:
        # Each quote gets its own id: one table item can hold a Contact Flow
        # announcement (excluded in ACXD only) next to the hand-off line the
        # plan must say, so exclusion has to be per sentence.
        for number, quote in enumerate(quotes, 1):
            quote["id"] = f"Q{number}"
        signals["quotes"] = quotes
    return signals


#: A quoted sentence a caller hears: "…" / “…” / 「…」 that ends like speech.
_QUOTED = re.compile(r"[\"“]([^\"“”]{10,500})[\"”]|「([^」]{6,500})」")
_SPEECH_END = re.compile(r"(?:[.?!。？！]|다|요|까|죠|니다|세요)$")
#: Words next to a quote that say the SYSTEM speaks it ("… 안내 후", "환영 메시지:").
_SPEECH_CUE = re.compile(r"안내|멘트|문구|메시지|말씀|고지|인사|환영|제안|announce|message|say|tell|notice|"
                         r"prompt|greet|repl|respond|案内|メッセージ|読み上げ", re.I)
#: Sections whose quotes are the CUSTOMER's test utterances, not system speech.
_EXAMPLE_SECTION = re.compile(r"기대\s*대화|예시\s*(발화|대화)|테스트|expected|sample\s+(dialog|conversation)|"
                              r"test\s+(case|dialog|conversation)|想定会話|テスト", re.I)


def quoted_sentences(items: Iterable[dict]) -> list[dict]:
    """Sentences the document puts in quotes for the system to SAY (a consent
    notice, a hand-off line): the customer expects them word for word. A quoted
    label or value ("기존 ERP", "벽걸이 실내기 대당 10만원") does not end like
    speech, and a caller's example utterance ("진료 예약하고 싶어요" in the
    expected-dialogue section) has no speech cue beside it; both are left out."""
    out: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if _EXAMPLE_SECTION.search(str(item.get("section") or "")):
            continue
        text = str(item.get("text") or "")
        for m in _QUOTED.finditer(text):
            sentence = " ".join((m.group(1) or m.group(2) or "").split())
            if not sentence or sentence in seen or not re.search(r"[A-Za-z\uac00-\ud7a3\u3040-\u30ff\u4e00-\u9fff]", sentence):
                continue
            if not _SPEECH_END.search(sentence.rstrip(" )")):
                continue
            if not _SPEECH_CUE.search(text[max(0, m.start() - 30):m.start()] + text[m.end():m.end() + 15]):
                continue
            seen.add(sentence)
            out.append({"text": sentence, "item_id": item.get("id")})
    return out


def _quote_key(text: str) -> str:
    return " ".join(str(text).replace("‘", "'").replace("’", "'").split())


def _is_excluded(mappings: dict, item_ids: Iterable[Any]) -> bool:
    return all(str((mappings.get(i) or {}).get("target", "")).startswith("excluded") for i in item_ids)


def kept_quotes(ledger: Optional[dict]) -> list[dict]:
    """The document's quoted sentences that must be said word for word: every
    tracked quote whose Q id and requirement item were not excluded."""
    if not ledger:
        return []
    mappings = ledger.get("mappings") or {}
    return [q for q in (ledger.get("signals") or {}).get("quotes") or []
            if isinstance(q, dict) and q.get("text")
            and not _is_excluded(mappings, [q.get("item_id")])
            and not _is_excluded(mappings, [q.get("id")])]


def _plan_texts(flow_spec: Any, *, include_contact_flow: bool = True) -> str:
    """Every place the ACXD builder takes a spoken sentence from — where a
    mandated sentence must appear to be spoken:

    * an operation plan, whole (its steps and rules feed the flow generator);
    * a SYSTEM plan's step ``template`` only — the deterministic builder speaks
      the approved template and nothing else, so a sentence left in the step's
      description was never said (SELC e2e, 2026-09-26);
    * a guardrail's ``message`` (its HandoffFlow and every journey say it);
    * the session greeting and closing;
    * the Contact Flow spec, when the builder generates a Contact Flow.
    """
    chunks: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, str):
            chunks.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                walk(v)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                walk(v)

    def dump(obj: Any) -> Any:
        return obj.model_dump() if hasattr(obj, "model_dump") else obj

    try:
        from tools.acxd_system_flows import is_system_flow_role
    except Exception:  # pragma: no cover - module optional in isolated tests
        def is_system_flow_role(role):  # type: ignore[misc]
            return False
    data = dump(flow_spec) or {}
    if not isinstance(data, dict):
        data = {}
    for plan in data.get("flows") or []:
        plan = dump(plan)
        if not isinstance(plan, dict):
            continue
        if is_system_flow_role(plan.get("role")):
            for step in plan.get("steps") or []:
                step = dump(step)
                if isinstance(step, dict) and step.get("template"):
                    chunks.append(str(step["template"]))
        else:
            walk(plan)
    for guard in data.get("guardrails") or []:
        guard = dump(guard)
        if isinstance(guard, dict) and guard.get("message"):
            chunks.append(str(guard["message"]))
    try:
        from tools.spec_manager import get_contact_flow_spec, get_session_flow_config
        config = get_session_flow_config()
        for key in ("common_greeting", "common_closing"):
            value = getattr(config, key, None) if config is not None else None
            if isinstance(value, str):
                chunks.append(value)
        if include_contact_flow:
            spec = get_contact_flow_spec()
            if spec is not None:
                walk(dump(spec))
    except Exception:
        pass
    return _quote_key("\n".join(chunks))


# ── enum values the interview re-typed ───────────────────────────────────────
#: A document token: a run of letters or digits (Hangul, kana, CJK, Latin).
_DOC_TOKEN = re.compile(r"[0-9A-Za-z_\uac00-\ud7a3\u3040-\u30ff\u4e00-\u9fff]+")


def _spec_enums(spec: Any):
    """(field name, enum values) for every field a spec declares, however nested."""
    stack = [spec.model_dump() if hasattr(spec, "model_dump") else spec]
    while stack:
        obj = stack.pop()
        if isinstance(obj, dict):
            values = obj.get("enum_values")
            if isinstance(values, list) and values:
                yield str(obj.get("name") or ""), values
            stack.extend(v for v in obj.values() if isinstance(v, (dict, list)))
        elif isinstance(obj, list):
            stack.extend(v for v in obj if isinstance(v, (dict, list)))


def enum_near_misses(document: str, specs: Any) -> list[dict]:
    """Enum values a spec carries that the customer's document never writes but
    writes ALMOST: '종합세추' where the document says '종합세척' (e2e 2026-09-26),
    '뱅걸이실내기' for '벽걸이실내기' (2026-09-25). The interview re-types enum
    values when it saves a spec, and a value one syllable off reaches the
    customer's API as is. Only same-length substitutions count (a retyping slip,
    not a variant such as '가정' / '가정용'); a value with no such neighbour in the
    document (one the customer added in conversation), a value shorter than four
    characters and one that differs from its neighbour only in digits pass."""
    text = str(document or "")
    if not text.strip():
        return []
    squeezed = re.sub(r"\s+", "", text)
    tokens = {t for t in _DOC_TOKEN.findall(text) if len(t) >= 4}
    out: list[dict] = []
    seen: set[str] = set()
    for op_id, spec in (specs or {}).items():
        for field, values in _spec_enums(spec):
            for value in values:
                v = str(value).strip()
                if len(v) < 4 or v in text or re.sub(r"\s+", "", v) in squeezed:
                    continue
                limit = 1 if len(v) <= 6 else 2
                best: Optional[tuple] = None
                for t in tokens:
                    if len(t) != len(v):
                        continue
                    diff = [(a, b) for a, b in zip(v, t) if a != b]
                    if not diff or len(diff) > limit or all(a.isdigit() and b.isdigit() for a, b in diff):
                        continue
                    if best is None or (len(diff), t) < best:
                        best = (len(diff), t)
                eid = f"E:{op_id}.{field}={v}"
                if best and eid not in seen:
                    seen.add(eid)
                    out.append({"id": eid, "operation": op_id, "field": field, "value": v, "document": best[1]})
    return out


def _ledger_document(ledger: dict) -> str:
    return "\n".join(str(i.get("text") or "") for i in ledger.get("items") or [])


def input_enum_conflicts(specs: Any) -> list[dict]:
    """Input fields that share a name across operations but not their enum values.

    Live (SELC, 2026-09-26): the interview corrected '종합세추' → '종합세척' in
    `create_cleaning_reservation.serviceType` only; `get_cleaning_price.serviceType`
    kept the typo, the price lookup's data request carried it, and D9 passed the
    bundle. One value the caller says fills both calls, so the sets must match —
    unless the customer really runs two vocabularies, which is excluded by the
    returned `X:<field>` id."""
    by_field: dict[str, dict[str, tuple]] = {}
    for op_id, spec in (specs or {}).items():
        fields = getattr(spec, "input_fields", None)
        if fields is None and isinstance(spec, dict):
            fields = spec.get("input_fields")
        for f in fields or []:
            name = getattr(f, "name", None) if not isinstance(f, dict) else f.get("name")
            values = getattr(f, "enum_values", None) if not isinstance(f, dict) else f.get("enum_values")
            if not name or not values:
                continue
            by_field.setdefault(str(name), {})[str(op_id)] = tuple(str(v).strip() for v in values)
    out: list[dict] = []
    for name, per_op in sorted(by_field.items()):
        if len(per_op) > 1 and len({frozenset(v) for v in per_op.values()}) > 1:
            out.append({"id": f"X:{name}", "field": name, "variants": {op: list(v) for op, v in per_op.items()}})
    return out


# ── storage ──────────────────────────────────────────────────────────────────
def _workspace(session_id: Optional[str] = None):
    try:
        if session_id:
            # D9 runs for a named session, not only the one bound to this turn.
            from tools.project_workspace import ProjectWorkspace, get_workspace_for
            return get_workspace_for(session_id) or ProjectWorkspace(session_id)
        from tools.project_workspace import ensure_workspace
        return ensure_workspace()
    except Exception as exc:  # pragma: no cover
        logger.debug("[requirement_items] workspace unavailable: %s", exc)
        return None


def load_ledger(session_id: Optional[str] = None) -> Optional[dict]:
    ws = _workspace(session_id) if session_id else _workspace()
    if ws is None:
        return None
    try:
        data = ws.load_requirement_items()
    except Exception as exc:
        logger.debug("[requirement_items] load failed: %s", exc)
        return None
    return data if isinstance(data, dict) and data.get("items") else None


def save_ledger(data: dict) -> bool:
    ws = _workspace()
    if ws is None:
        return False
    try:
        ws.save_requirement_items(data)
        return True
    except Exception as exc:
        logger.warning("[requirement_items] save failed: %s", exc)
        return False


def register_document(text: str) -> Optional[dict]:
    """Split the saved raw_input into items and start (or reset) the ledger.
    Returns the ledger summary, or None when the workspace is unavailable."""
    items = split_requirement_items(text)
    if not items:
        return None
    ledger = {"items": items, "mappings": {}, "signals": requirement_signals(items)}
    if not save_ledger(ledger):
        return None
    return ledger_summary(ledger)


def ledger_summary(ledger: dict) -> dict:
    items = ledger.get("items") or []
    mappings = ledger.get("mappings") or {}
    unmapped = [i["id"] for i in items if i["id"] not in mappings]
    return {"item_count": len(items), "mapped": len(items) - len(unmapped), "unmapped": unmapped,
            "signals": ledger.get("signals") or {}}


# ── coverage ─────────────────────────────────────────────────────────────────
def _spec_corpus() -> dict[str, str]:
    """Identifiers the specs carry, so an item that names one is covered by it."""
    corpus: dict[str, str] = {}
    try:
        from tools.spec_manager import get_all_specs
        for op_id, spec in (get_all_specs() or {}).items():
            corpus[str(op_id)] = f"operation:{op_id}"
            for f in list(getattr(spec, "input_fields", None) or []) + list(getattr(spec, "output_fields", None) or []):
                name = getattr(f, "name", None) or (f.get("name") if isinstance(f, dict) else None)
                if name:
                    corpus[str(name)] = f"field:{op_id}.{name}"
            for t in getattr(spec, "tools", None) or []:
                tid = getattr(t, "tool_id", None) or (t.get("tool_id") if isinstance(t, dict) else None)
                if tid:
                    corpus[str(tid)] = f"operation:{op_id}"
    except Exception as exc:  # pragma: no cover
        logger.debug("[requirement_items] spec corpus unavailable: %s", exc)
    try:
        from tools.acxd_flow_spec import get_acxd_flow_spec
        fs = get_acxd_flow_spec()
        for f in (getattr(fs, "flows", None) or []):
            corpus[str(f.flow_id)] = f"flow:{f.flow_id}"
        for topic in (getattr(getattr(fs, "knowledge_base", None), "topics", None) or []):
            corpus[str(topic)] = "kb"
    except Exception:
        pass
    return {k: v for k, v in corpus.items() if len(k) >= 3}


def _auto_target(text: str, corpus: dict[str, str]) -> Optional[str]:
    """An item that spells a spec identifier (operation id, field name, flow id,
    KB topic) is covered by that thing — no mapping call needed."""
    lowered = text.lower()
    hits = [(len(k), v) for k, v in corpus.items()
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(k.lower())}(?![A-Za-z0-9_])", lowered)]
    if not hits:
        return None
    hits.sort(reverse=True)
    return hits[0][1]


def items_with_status(ledger: dict) -> list[dict]:
    corpus = _spec_corpus()
    mappings = ledger.get("mappings") or {}
    out: list[dict] = []
    for item in ledger.get("items") or []:
        row = dict(item)
        mapped = mappings.get(item["id"])
        if mapped:
            row["status"] = "excluded" if str(mapped.get("target", "")).startswith("excluded") else "mapped"
            row["target"] = mapped.get("target")
            row["note"] = mapped.get("note")
        else:
            auto = _auto_target(str(item.get("text") or ""), corpus)
            if auto:
                row["status"], row["target"] = "auto", auto
            else:
                row["status"] = "unmapped"
        out.append(row)
    return out


def _acxd_context(session_id: Optional[str]) -> tuple[bool, Any]:
    try:
        from tools.acxd_flow_spec import get_acxd_flow_spec, is_acxd_target
        if session_id and is_acxd_target(session_id):
            return True, get_acxd_flow_spec()
    except Exception:
        pass
    return False, None


def _is_acxd_only(session_id: Optional[str]) -> bool:
    try:
        from tools.acxd_flow_spec import is_acxd_only_target
        return bool(session_id) and is_acxd_only_target(session_id)
    except Exception:
        return False


def requirement_coverage_problems(session_id: Optional[str] = None) -> list[str]:
    """What the document says that no spec expresses. Empty when there is no
    document or everything is accounted for."""
    ledger = load_ledger()
    if not ledger:
        return []
    problems: list[str] = []
    rows = items_with_status(ledger)
    unmapped = [r for r in rows if r["status"] == "unmapped"]
    if unmapped:
        shown = "; ".join(f"{r['id']} \"{str(r['text'])[:60]}\"" for r in unmapped[:8])
        more = f" (+{len(unmapped) - 8} more)" if len(unmapped) > 8 else ""
        problems.append(
            f"{len(unmapped)} requirement item(s) are reflected in no spec: {shown}{more} — "
            "map each with map_requirement_items (operation:/field:/flow:/kb/guardrail/session_config/"
            "contact_flow/infrastructure/persona) or record 'excluded' with the customer's reason; "
            "list_requirement_items(status='unmapped') shows them")

    signals = ledger.get("signals") or {}
    mappings = ledger.get("mappings") or {}

    def excluded(item_ids: Iterable[str]) -> bool:
        return all(str((mappings.get(i) or {}).get("target", "")).startswith("excluded") for i in item_ids)

    lookup = signals.get("phone_lookup")
    # ACXD only has no Contact Flow spec and withholds save_contact_flow_spec, so
    # this check could only be cleared by excluding the item (live on dev,
    # 2026-09-25: one refused complete_interview round). The item is still
    # mapped like every other one by the unmapped check above.
    if lookup and not excluded([lookup.get("item_id")]) and not _is_acxd_only(session_id):
        try:
            from tools.spec_manager import get_contact_flow_spec
            cf = get_contact_flow_spec()
            actual = bool(getattr(cf, "include_customer_phone_lookup", False)) if cf else None
        except Exception:
            actual = None
        if actual != bool(lookup["value"]):
            problems.append(
                f"the document sets include_customer_phone_lookup={str(lookup['value']).lower()} ({lookup['item_id']}) "
                f"but the contact flow spec has {actual if actual is not None else 'no value'} — "
                "save_contact_flow_spec(include_customer_phone_lookup=...) as the document says, or record "
                "the item as excluded with the customer's reason")

    is_acxd, flow_spec = _acxd_context(session_id)
    faq = signals.get("faq")
    if faq and is_acxd and not excluded(faq["item_ids"]):
        topics = list(getattr(getattr(flow_spec, "knowledge_base", None), "topics", None) or []) if flow_spec else []
        if not topics:
            problems.append(
                f"the document has an FAQ section ({', '.join(faq['item_ids'][:6])}) but the ACXD knowledge base "
                "plans no topics — save_acxd_policies(kb_name=..., kb_topics=[...]) with those topics (the FAQ "
                "asset and the FAQ flow are generated from them), or record the items as excluded")

    identity = signals.get("identity_verification")
    if identity and is_acxd and flow_spec is not None and not excluded(identity["item_ids"]):
        words = re.compile(r"본인|인증|verif|identity|authenticat|本人", re.I)
        has_step = any(
            words.search(str(getattr(st, "description", "") or ""))
            for f in (getattr(flow_spec, "flows", None) or []) for st in (getattr(f, "steps", None) or []))
        if not has_step:
            problems.append(
                f"the document asks for identity verification ({', '.join(identity['item_ids'][:4])}) but no flow "
                "step describes verifying the caller — plan the verification steps (user_choice for the value the "
                "customer proves, data_request to check it, choice → escalate after the allowed misses), or record "
                "the items as excluded with the customer's reason")
    # Quoted sentences are said word for word (e2e 2026-09-25: the document's
    # "이전 전달주신 정보는 …" hand-off line reached the plan as "이전에 전달해 주신
    # 정보는 …" while the interview reported it verbatim). Plans only: a
    # sentence in an operation's business rules is not yet said by any step.
    quotes = signals.get("quotes") or []
    if quotes and is_acxd and flow_spec is not None:
        # ACXD only ships no Contact Flow: nothing there is ever said.
        haystack = _plan_texts(flow_spec, include_contact_flow=not _is_acxd_only(session_id))
        missing = [q for q in kept_quotes(ledger) if _quote_key(q["text"]) not in haystack]
        if missing:
            shown = "; ".join(f"{q.get('id')} ({q.get('item_id')}) \"{q['text'][:80]}\"" for q in missing[:6])
            more = f" (+{len(missing) - 6} more)" if len(missing) > 6 else ""
            problems.append(
                f"the document quotes {len(missing)} sentence(s) that no plan step says verbatim: {shown}{more} — "
                "copy each, character for character, into the template or wording of the step that says it (do "
                "not polish the grammar; in a welcome, fallback or escalation plan only the step's `template` is "
                "spoken), or record that quote (its Q id) as excluded with the reason (e.g. said by the customer's "
                "own Contact Flow)")
    # Enum values reach the customer's API exactly as saved, and the interview
    # re-types them from the document (see enum_near_misses).
    try:
        from tools.spec_manager import get_all_specs
        typos = [e for e in enum_near_misses(_ledger_document(ledger), get_all_specs())
                 if not excluded([e["id"]])]
    except Exception as exc:
        logger.debug("[requirement_items] enum check skipped: %s", exc)
        typos = []
    if typos:
        shown = "; ".join(f"{e['operation']}.{e['field']} '{e['value']}' (the document writes '{e['document']}')"
                          for e in typos[:6])
        problems.append(
            f"{len(typos)} enum value(s) differ from the document by a character or two: {shown} — re-save the "
            "operation spec with the document's spelling, or, only when the customer asked for that exact value, "
            "record its id (" + ", ".join(e["id"] for e in typos[:3]) + ") as excluded with the customer's reason")
    # One spoken value fills every call that shares the field, and ACXD keeps ONE
    # slot type per name for the whole application (see input_enum_conflicts).
    # Classic operations each own their Lambda and OpenAPI, so a per-operation
    # vocabulary is legitimate there.
    try:
        from tools.spec_manager import get_all_specs
        conflicts = ([c for c in input_enum_conflicts(get_all_specs()) if not excluded([c["id"]])]
                     if is_acxd else [])
    except Exception as exc:
        logger.debug("[requirement_items] enum conflict check skipped: %s", exc)
        conflicts = []
    if conflicts:
        shown = "; ".join(
            f"{c['field']}: " + ", ".join(f"{op} {v}" for op, v in c["variants"].items()) for c in conflicts[:4])
        problems.append(
            f"{len(conflicts)} input field(s) carry different enum values in different operations: {shown} — "
            "save the same values in every operation that takes the field, or, only when the customer really uses "
            "two vocabularies, record " + ", ".join(c["id"] for c in conflicts[:3]) + " as excluded with the reason")
    return problems


# ── tools ────────────────────────────────────────────────────────────────────
@tool
def list_requirement_items(status: str = "unmapped") -> dict:
    """
    The customer's requirements document, item by item, with what each item became.

    Call it before complete_interview: every item must be `mapped` (a spec expresses
    it), `auto` (the item names a spec identifier — covered) or `excluded` (the
    customer agreed to leave it out). `unmapped` items block the interview.

    Args:
        status: 'unmapped' (default), 'all', 'mapped', 'auto' or 'excluded'.

    Returns:
        items with id, section, text, status, target and the document's literal signals
        (phone lookup flag, FAQ section, identity verification, failure count).
    """
    ledger = load_ledger()
    if not ledger:
        return {"success": True, "items": [], "message": "No requirements document has been saved in this session."}
    rows = items_with_status(ledger)
    if status != "all":
        rows = [r for r in rows if r["status"] == status]
    return {"success": True, "count": len(rows), "items": rows, "signals": ledger.get("signals") or {},
            "summary": ledger_summary(ledger)}


@tool
def map_requirement_items(mappings: Union[list[dict], str]) -> dict:
    """
    Record what each requirement item became — the spec that expresses it — or
    that the customer excluded it.

    Args:
        mappings: [{"item_id": "R12", "target": "field:create_reservation.preferredDate", "note": "..."}].
            target is one of: operation:<operation_id>, field:<operation_id>.<field_name>,
            flow:<flow_id>, kb, guardrail, session_config, contact_flow, infrastructure, persona,
            excluded (note = the customer's reason, required). One item may be mapped again later;
            the latest mapping wins.

    Returns:
        The updated coverage summary; unknown item ids and malformed targets are reported and skipped.
    """
    # The model sometimes sends the list JSON-encoded as one string; live (Hanbit,
    # 2026-09-26) that failed validation, showed the raw escaped JSON in the chat
    # and cost a retry. Accept it rather than bounce it.
    if isinstance(mappings, str):
        try:
            mappings = json.loads(mappings)
        except ValueError:
            return {"success": False,
                    "error": "mappings must be a list of {item_id, target, note} objects"}
    if isinstance(mappings, dict):
        mappings = [mappings]
    if not isinstance(mappings, list):
        return {"success": False, "error": "mappings must be a list of {item_id, target, note} objects"}
    ledger = load_ledger()
    if not ledger:
        return {"success": False, "error": "No requirements document has been saved in this session."}
    known_ids = {i["id"] for i in ledger.get("items") or []}
    known_ids |= {str(q.get("id")) for q in ((ledger.get("signals") or {}).get("quotes") or []) if q.get("id")}
    try:  # an enum value the customer really asked for is excluded by its E: id
        from tools.spec_manager import get_all_specs
        known_ids |= {e["id"] for e in enum_near_misses(_ledger_document(ledger), get_all_specs())}
        known_ids |= {c["id"] for c in input_enum_conflicts(get_all_specs())}
    except Exception as exc:
        logger.debug("[requirement_items] enum ids unavailable: %s", exc)
    stored = dict(ledger.get("mappings") or {})
    rejected: list[str] = []
    try:
        from tools.spec_manager import get_all_specs
        known_ops = set((get_all_specs() or {}).keys())
    except Exception:
        known_ops = set()
    for m in mappings or []:
        if not isinstance(m, dict):
            rejected.append(f"{m!r}: not an object")
            continue
        item_id = str(m.get("item_id") or "").strip()
        target = str(m.get("target") or "").strip()
        note = str(m.get("note") or "").strip()
        kind = target.split(":", 1)[0]
        if item_id not in known_ids:
            rejected.append(f"{item_id or '?'}: unknown item id")
            continue
        if kind not in TARGET_KINDS or (kind in ("operation", "field", "flow") and ":" not in target):
            rejected.append(f"{item_id}: target {target!r} is not one of {TARGET_KINDS}")
            continue
        if kind == "excluded" and not note:
            rejected.append(f"{item_id}: 'excluded' needs the customer's reason in note")
            continue
        if kind in ("operation", "field") and known_ops:
            op = target.split(":", 1)[1].split(".", 1)[0]
            if op not in known_ops:
                rejected.append(f"{item_id}: {target!r} names no saved OperationSpec ({sorted(known_ops)})")
                continue
        stored[item_id] = {"target": target, "note": note}
    ledger["mappings"] = stored
    if not save_ledger(ledger):
        return {"success": False, "error": "ledger could not be saved"}
    summary = ledger_summary(ledger)
    return {"success": True, "summary": summary, "rejected": rejected,
            "remaining_unmapped": [r["id"] for r in items_with_status(ledger) if r["status"] == "unmapped"]}
