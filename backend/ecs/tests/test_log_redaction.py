"""The WebSocket URL's Cognito token never reaches the log (dev, 2026-09-25)."""

from __future__ import annotations

import logging
import os
import sys

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

JWT = "eyJraWQiOiJTZXo.eyJzdWIiOiIyNDk4.c2lnbmF0dXJl"


def test_uvicorn_connection_lines_lose_the_token_but_keep_the_rest(caplog):
    from tools.log_redaction import install_query_token_redaction

    install_query_token_redaction()
    install_query_token_redaction()              # idempotent
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        logging.getLogger("uvicorn.error").info('%s - "WebSocket %s" [accepted]', "10.0.1.254:64128",
                                                f"/ws?token={JWT}&sessionId=session-1")
        logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "10.0.0.1:1", "GET",
                                                 f"/api/x?id_token={JWT}", "1.1", 200)
        logging.getLogger("uvicorn.error").info(f"plain message /ws?token={JWT}")
    text = caplog.text
    assert JWT not in text
    assert "/ws?token=[REDACTED]&sessionId=session-1" in text
    assert "id_token=[REDACTED]" in text and "[accepted]" in text
    assert len([f for f in logging.getLogger("uvicorn.error").filters
                if type(f).__name__ == "QueryTokenRedactionFilter"]) == 1
