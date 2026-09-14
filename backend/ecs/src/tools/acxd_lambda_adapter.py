"""Restore field formats at the ACXD → backend boundary.

Live (Hanbit, 2026-09-14): the Agentic CX Designer runtime delivers built-in slot
values without their separators — ``NLX.PhoneNumber`` "010-1111-2222" arrived as
``01011112222``, ``NLX.AlphaNumeric`` "2026-09-16" as ``20260916`` and "10:00" as
``1000`` — while the generated Lambda validates the OperationSpec's format
(``^010-\\d{4}-\\d{4}$``) and rejected every booking.

The Lambda is the one place that knows both the incoming value and the expected
format, so an ACXD bundle's handlers get a small deterministic wrapper: for
every request field with a fixed-shape pattern, a value that does not match but
whose alphanumeric content fits the pattern's skeleton is rebuilt with the
separators put back. Values that already match, or do not fit, are untouched.

Only fixed-shape patterns are handled — literal prefixes/separators plus
``\\d{n}`` / ``[0-9]{n}`` / ``[A-Z]{n}`` / ``[A-Za-z0-9]{n}`` runs. Anything else
(alternations, optional groups, unbounded quantifiers) is left alone.
"""
from __future__ import annotations

import json
import re
from typing import Optional

MARKER = "# --- AICC Builder: ACXD delivers slot values without separators ---"

_RUN = re.compile(
    r"\\d\{(\d+)\}|\[0-9\]\{(\d+)\}|\[A-Z\]\{(\d+)\}|\[a-z\]\{(\d+)\}|"
    r"\[A-Za-z0-9\]\{(\d+)\}|\[A-Za-z\]\{(\d+)\}|\\d"
)
_LITERAL = re.compile(r"\\([-.:/ ])|([A-Za-z0-9])|([-:/. ])")
_SEPARATORS = set("-:/. ")


def skeleton(pattern: str) -> Optional[list[tuple[str, str]]]:
    """Parse a fixed-shape regex into [("lit", "010"), ("sep", "-"), ("run", "dddd"), …].

    Returns None when the pattern uses anything beyond literals, separators and
    fixed-width character runs.
    """
    if not isinstance(pattern, str) or not pattern:
        return None
    body = pattern
    if body.startswith("^"):
        body = body[1:]
    if body.endswith("$"):
        body = body[:-1]
    parts: list[tuple[str, str]] = []
    pos = 0
    while pos < len(body):
        run = _RUN.match(body, pos)
        if run:
            width = next((g for g in run.groups() if g), None)
            count = int(width) if width else 1
            kind = "A" if "A-Z" in run.group(0) or "a-z" in run.group(0) else "d"
            parts.append(("run", kind * count))
            pos = run.end()
            continue
        lit = _LITERAL.match(body, pos)
        if lit:
            escaped, alnum, sep = lit.groups()
            if escaped is not None:
                parts.append(("sep", escaped))
            elif alnum is not None:
                if parts and parts[-1][0] == "lit":
                    parts[-1] = ("lit", parts[-1][1] + alnum)
                else:
                    parts.append(("lit", alnum))
            else:
                parts.append(("sep", sep))
            pos = lit.end()
            continue
        return None  # unsupported construct
    if not any(kind == "sep" for kind, _ in parts):
        return None  # nothing to restore
    return parts


def restore(value: str, pattern: str) -> Optional[str]:
    """The value with separators re-inserted per ``pattern``, or None."""
    if not isinstance(value, str):
        return None
    try:
        if re.fullmatch(pattern, value):
            return None
    except re.error:
        return None
    parts = skeleton(pattern)
    if parts is None:
        return None
    compact = "".join(ch for ch in value if ch not in _SEPARATORS)
    expected = sum(len(t) for k, t in parts if k != "sep")
    if len(compact) == expected - 1 and compact.isdigit() and ":" in pattern:
        compact = "0" + compact  # "930" → "0930": a dropped leading zero (times only)
    out: list[str] = []
    pos = 0
    for kind, text in parts:
        if kind == "sep":
            out.append(text)
            continue
        if kind == "lit":
            if compact[pos:pos + len(text)].upper() != text.upper():
                return None
            out.append(text)
            pos += len(text)
            continue
        width = len(text)
        chunk = compact[pos:pos + width]
        if len(chunk) != width:
            return None
        if text[0] == "d" and not chunk.isdigit():
            return None
        if text[0] == "A" and not chunk.isalnum():
            return None
        out.append(chunk)
        pos += width
    if pos != len(compact):
        return None
    candidate = "".join(out)
    try:
        return candidate if re.fullmatch(pattern, candidate) else None
    except re.error:
        return None


def restorable_patterns(field_patterns: dict) -> dict:
    """Keep only the fields whose pattern has a fixed shape with separators."""
    return {name: pat for name, pat in (field_patterns or {}).items()
            if isinstance(name, str) and isinstance(pat, str) and skeleton(pat) is not None}


_WRAPPER = '''

{marker}
# The Agentic CX Designer runtime delivers built-in slot values without their
# separators (010-1111-2222 -> 01011112222, 2026-09-16 -> 20260916, 10:00 -> 1000).
# Rebuild the OperationSpec formats before the handler validates them.
import json as _aicc_json
import re as _aicc_re

_AICC_FIELD_PATTERNS = {patterns}
_AICC_RESPONSE_TYPES = {response_types}
_AICC_SEPARATORS = set("-:/. ")
_AICC_RUN = _aicc_re.compile(r"\\\\d\\{{(\\d+)\\}}|\\[0-9\\]\\{{(\\d+)\\}}|\\[A-Z\\]\\{{(\\d+)\\}}|\\[a-z\\]\\{{(\\d+)\\}}|\\[A-Za-z0-9\\]\\{{(\\d+)\\}}|\\[A-Za-z\\]\\{{(\\d+)\\}}|\\\\d")
_AICC_LITERAL = _aicc_re.compile(r"\\\\([-.:/ ])|([A-Za-z0-9])|([-:/. ])")


def _aicc_skeleton(pattern):
    body = pattern[1:] if pattern.startswith("^") else pattern
    body = body[:-1] if body.endswith("$") else body
    parts, pos = [], 0
    while pos < len(body):
        run = _AICC_RUN.match(body, pos)
        if run:
            width = next((g for g in run.groups() if g), None)
            kind = "A" if ("A-Z" in run.group(0) or "a-z" in run.group(0)) else "d"
            parts.append(("run", kind * (int(width) if width else 1)))
            pos = run.end()
            continue
        lit = _AICC_LITERAL.match(body, pos)
        if not lit:
            return None
        escaped, alnum, sep = lit.groups()
        if escaped is not None or sep is not None:
            parts.append(("sep", escaped if escaped is not None else sep))
        elif parts and parts[-1][0] == "lit":
            parts[-1] = ("lit", parts[-1][1] + alnum)
        else:
            parts.append(("lit", alnum))
        pos = lit.end()
    return parts if any(k == "sep" for k, _ in parts) else None


def _aicc_restore(value, pattern):
    if not isinstance(value, str) or _aicc_re.fullmatch(pattern, value):
        return None
    parts = _aicc_skeleton(pattern)
    if parts is None:
        return None
    compact = "".join(ch for ch in value if ch not in _AICC_SEPARATORS)
    # A typed "9:30" reaches the slot as "930": one digit short of the skeleton
    # means a dropped leading zero (times, never ids), so pad it back.
    expected = sum(len(t) for k, t in parts if k != "sep")
    if len(compact) == expected - 1 and compact.isdigit() and ":" in pattern:
        compact = "0" + compact
    out, pos = [], 0
    for kind, text in parts:
        if kind == "sep":
            out.append(text)
            continue
        if kind == "lit":
            if compact[pos:pos + len(text)].upper() != text.upper():
                return None
            out.append(text)
            pos += len(text)
            continue
        chunk = compact[pos:pos + len(text)]
        if len(chunk) != len(text) or (text[0] == "d" and not chunk.isdigit()) or not chunk.isalnum():
            return None
        out.append(chunk)
        pos += len(text)
    if pos != len(compact):
        return None
    candidate = "".join(out)
    return candidate if _aicc_re.fullmatch(pattern, candidate) else None


def _aicc_restore_formats(event):
    try:
        body = event.get("body") if isinstance(event, dict) else None
        data = _aicc_json.loads(body) if isinstance(body, str) else body
        if not isinstance(data, dict):
            return event
        changed = False
        for name, pattern in _AICC_FIELD_PATTERNS.items():
            fixed = _aicc_restore(data.get(name), pattern)
            if fixed is not None:
                data[name] = fixed
                changed = True
        if changed:
            event = dict(event)
            event["body"] = _aicc_json.dumps(data, ensure_ascii=False) if isinstance(body, str) else data
    except Exception:  # never break the handler over a formatting aid
        return event
    return event


def _aicc_normalize_response(result):
    """ACXD treats the webhook reply as a success only for HTTP 200 with a body
    that matches the Data Request's responseSchema: a 201 from a create
    operation, or `errorCode: null` where the schema says string, sends the
    conversation down the failure branch (live). Classic callers never see this."""
    try:
        if not isinstance(result, dict):
            return result
        if result.get("statusCode") in (201, 202, 204):
            result = dict(result)
            result["statusCode"] = 200
        body = result.get("body")
        data = _aicc_json.loads(body) if isinstance(body, str) else body
        if isinstance(data, dict):
            changed = False
            for key in ("errorCode", "message"):
                if key in data and data[key] is None:
                    data[key] = ""
                    changed = True
            if "success" in data and not isinstance(data["success"], bool):
                data["success"] = bool(data["success"])
                changed = True
            # The Data Request's responseSchema is validated by the service: a
            # numeric field returned as the string "45000" fails the whole reply
            # (live: the order lookup went silent). Coerce to the schema's type.
            for key, kind in _AICC_RESPONSE_TYPES.items():
                if key not in data:
                    continue
                value = data[key]
                coerced = _aicc_coerce(value, kind)
                if coerced is not value:
                    data[key] = coerced
                    changed = True
            if changed:
                result = dict(result)
                result["body"] = _aicc_json.dumps(data, ensure_ascii=False) if isinstance(body, str) else data
    except Exception:
        return result
    return result


def _aicc_coerce(value, kind):
    try:
        if kind in ("number", "integer"):
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, (int, float)):
                return int(value) if kind == "integer" and float(value).is_integer() else value
            if isinstance(value, str):
                text = value.strip().replace(",", "")
                if kind == "integer" and text.lstrip("-").isdigit():
                    return int(text)
                number = float(text)
                return int(number) if kind == "integer" or number.is_integer() else number
        elif kind == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0", "yes", "no", "y", "n"):
                return value.strip().lower() in ("true", "1", "yes", "y")
        elif kind == "string":
            if value is None:
                return ""
            if isinstance(value, bool):
                return "true" if value else "false"
            if isinstance(value, (int, float)):
                return str(value)
    except (TypeError, ValueError):
        return value
    return value


_aicc_wrapped_handler = {handler}


def {handler}(event, context):
    return _aicc_normalize_response(_aicc_wrapped_handler(_aicc_restore_formats(event), context))
'''


def inject(code: str, field_patterns: dict, handler: Optional[str] = None,
           response_types: Optional[dict] = None) -> tuple[str, list[str]]:
    """Append the format-restoring wrapper to a Python handler source.

    Returns (new_code, restored_fields). The code is returned unchanged when no
    field has a restorable pattern, when no handler function (``lambda_handler``
    or ``handler``) is defined, or when the wrapper is already present.
    """
    if not isinstance(code, str) or MARKER in code:
        return code, []
    patterns = restorable_patterns(field_patterns)
    # The response normalisation applies even when no field has a format.
    candidates = [handler] if handler else ["lambda_handler", "handler"]
    handler = next((name for name in candidates
                    if name and re.search(rf"^def {re.escape(name)}\s*\(", code, re.M)), None)
    if handler is None:
        return code, []
    types = {str(k): str(v) for k, v in (response_types or {}).items()
             if v in ("number", "integer", "boolean", "string")}
    wrapper = _WRAPPER.format(
        marker=MARKER,
        patterns=json.dumps(patterns, ensure_ascii=False),
        response_types=json.dumps(types, ensure_ascii=False),
        handler=handler,
    )
    return code.rstrip("\n") + "\n" + wrapper, sorted(patterns) or ["<response>"]
