"""ASCII-only ACXD metadata must not keep fragments of a Korean sentence.

Hanbit e2e (2026-09-27): the bookAppointment Data Request purpose
"진료 예약 접수 (병원 기존 API)" reached the bundle as "( API)".
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from tools.acxd_bundle import enforce_ascii_metadata  # noqa: E402
from tools.acxd_data_request_builder import build_data_request  # noqa: E402


def test_a_fragment_left_by_korean_is_dropped_and_real_english_is_kept():
    assert enforce_ascii_metadata({"description": "진료 예약 접수 (병원 기존 API)"}) == {}
    assert enforce_ascii_metadata({"description": "한빛병원 AI 상담원 (ACXD 전용, API 연동)"}) == {}
    kept = enforce_ascii_metadata({"description": "Booking API for 한빛 hospital appointments"})
    assert kept == {"description": "Booking API for hospital appointments"}
    assert enforce_ascii_metadata({"description": "Looks up an order"}) == {"description": "Looks up an order"}


def test_a_korean_data_request_purpose_becomes_an_ascii_sentence():
    doc = build_data_request({"data_request_id": "bookAppointment", "purpose": "진료 예약 접수 (병원 기존 API)",
                              "mode": "external", "url": "{WEBHOOK_URL}/v1/appointments",
                              "request_fields": [], "response_fields": []})
    assert doc["description"] == "Calls the bookAppointment operation of the backend API."
    english = build_data_request({"data_request_id": "lookupOrder", "purpose": "Look up an order by number",
                                  "mode": "external", "url": "{WEBHOOK_URL}/v1/orders",
                                  "request_fields": [], "response_fields": []})
    assert english["description"] == "Look up an order by number"
