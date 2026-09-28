"""Keep bearer tokens out of the server logs.

The dashboard opens its WebSocket as ``/ws?token=<Cognito JWT>``, and uvicorn
logs every connection with the full path (``"WebSocket /ws?token=eyJ…"
[accepted]``) — so anyone who can read the CloudWatch log group could replay a
live session until the token expires (seen on dev, 2026-09-25). The filter
rewrites the value of token-like query parameters before any handler formats
the record; the rest of the line is kept for debugging.
"""

from __future__ import annotations

import logging
import re

_TOKEN_PARAM = re.compile(r"([?&](?:token|access_token|id_token|jwt|auth)=)[^&\s\"'\]]+", re.IGNORECASE)
_REDACTED = r"\1[REDACTED]"

#: The loggers uvicorn writes request/connection lines to.
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def redact_query_tokens(text: str) -> str:
    return _TOKEN_PARAM.sub(_REDACTED, text) if isinstance(text, str) and "=" in text else text


class QueryTokenRedactionFilter(logging.Filter):
    """Redacts ``?token=…``-style values in a record's message and arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact_query_tokens(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(redact_query_tokens(a) for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: redact_query_tokens(v) for k, v in record.args.items()}
        except Exception:  # pragma: no cover - a log line must never fail
            pass
        return True


def install_query_token_redaction(names: tuple[str, ...] = UVICORN_LOGGERS) -> None:
    """Attach the filter once to each named logger (idempotent)."""
    for name in names:
        target = logging.getLogger(name)
        if not any(isinstance(f, QueryTokenRedactionFilter) for f in target.filters):
            target.addFilter(QueryTokenRedactionFilter())
