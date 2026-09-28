"""J11 — journeys count dates in the business's time zone.

Live (Workshop Studio sandbox, 2026-09-28 09:28 Seoul time): the ACXD runtime
told the booking journey "Now it is Sunday, 2026-09-27 8:28 PM
(America/New_York)", so at 00:10 Seoul time "내일" became the same day and the
backend refused it. Told the business's zone the journey said "오늘 2026-09-28
(월요일)" and booked "내일 오전 10시" on 2026-09-29. The same run booked "이번 주
금요일" with no read-back (the [tool use] line put the booking under "a lookup is
called at once"), and Haiku made that Friday October 3rd.
"""

from __future__ import annotations

import copy
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.abspath(os.path.join(_HERE, "..", "src"))):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from tools.acxd_runtime_contract import _RuntimeContract, apply_runtime_contract  # noqa: E402
from tools.acxd_timezone import (  # noqa: E402
    application_timezone, business_timezone, canonical_timezone, offset_summary,
)
from test_acxd_runtime_contract import _apply_journey, _journey_flow, _journey_kwargs  # noqa: E402

_CARRYING = {"tools": [{"type": "dataRequest", "dataRequest": {"dataRequestId": "requestReturn"}}]}


def _prompt(flow: dict) -> str:
    return flow["nodes"]["gj"]["metadata"]["generativeJourney"]["prompt"]


def _old_lines() -> list[str]:
    return [line for table in (_RuntimeContract._STYLE_RELATIVE_DATE_LINE,
                               _RuntimeContract._TOOL_RELATIVE_DATE_LINE) for line in table.values()]


def test_a_korean_journey_counts_dates_in_seoul_and_reads_them_back():
    out, notes = _apply_journey(_journey_flow())
    prompt = _prompt(out)
    assert "[date basis]" in prompt
    assert "Asia/Seoul(UTC+9, 서머타임 없음)" in prompt and "(예: America/New_York)" in prompt
    assert "'M월 D일 (요일)'로 되읽고, 고객이 맞다고 답한 뒤에 씁니다" in prompt
    # live: "말씀하신 오늘은 서울 기준으로 9월 28일" — the basis is not the caller's business
    assert "시간대는 고객이 묻지 않으면 말하지 않습니다." in prompt
    assert not any(line in prompt for line in _old_lines())
    assert any("(J11)" in n for n in notes)
    again, notes_again = _apply_journey(copy.deepcopy(out))
    assert _prompt(again) == prompt and prompt.count("[date basis]") == 1
    assert not any("(J11)" in n for n in notes_again)


def test_a_carrying_journey_asks_for_a_yes_before_it_books():
    out, _ = _apply_journey(_journey_flow(journey_cfg=copy.deepcopy(_CARRYING)))
    prompt = _prompt(out)
    assert "[tool use]" in prompt and "[date basis]" in prompt
    assert "- 도구: requestReturn. 조회(읽기) 도구는 필요한 값이 모이면 바로 호출합니다." in prompt
    assert "고객이 '네'라고 답한 뒤에만 호출합니다" in prompt and "'예약해 주세요'라고 말했어도" in prompt
    assert "조회(읽기)는 필요한 값이 모이면 바로 requestReturn" not in prompt
    assert not any(line in prompt for line in _old_lines())


def test_a_bundle_packaged_earlier_is_upgraded_in_place():
    first, _ = _apply_journey(_journey_flow(journey_cfg=copy.deepcopy(_CARRYING)), timezone=None)
    stale = copy.deepcopy(first)
    cfg = stale["nodes"]["gj"]["metadata"]["generativeJourney"]
    # the prompt as the 2026-09-27 builder wrote it: old call line, both old date lines, no [date basis]
    old_call = _RuntimeContract._OLD_TOOL_CALL_RULE["ko"].format(names="requestReturn")
    new_call = _RuntimeContract._TOOL_CALL_RULE["ko"].format(names="requestReturn")
    text = cfg["prompt"]
    text = re.sub(r"\n\n\[date basis\][\s\S]*?(?=\n\n\[|$)", "", text).replace(new_call, old_call)
    for line in (_RuntimeContract._STYLE_RELATIVE_DATE_LINE["ko"], _RuntimeContract._TOOL_RELATIVE_DATE_LINE["ko"]):
        if line not in text:
            text = text.replace("[conversation style]\n", "[conversation style]\n" + line + "\n", 1)
    cfg["prompt"] = text
    assert old_call in text and "[date basis]" not in text
    out, notes = _apply_journey(stale)
    prompt = _prompt(out)
    assert new_call in prompt and old_call not in prompt
    assert prompt.count("[date basis]") == 1 and not any(line in prompt for line in _old_lines())
    assert any("yes to the read-back" in n for n in notes) and any("(J11)" in n for n in notes)


def test_the_zone_follows_the_language_or_what_the_interview_recorded():
    ja = _journey_flow()
    ja["mainLanguageCode"] = "ja-JP"
    out_ja, _ = _apply_journey(ja)
    assert "基準タイムゾーンは Asia/Tokyo（UTC+9、夏時間なし）" in _prompt(out_ja)

    en = _journey_flow()
    en["mainLanguageCode"] = "en-US"
    out_en, notes_en = _apply_journey(en)
    assert "[date basis]" not in _prompt(out_en)                       # no zone known: unchanged
    assert _RuntimeContract._STYLE_RELATIVE_DATE_LINE["en"] in _prompt(out_en)
    assert not any("(J11)" in n for n in notes_en)

    chicago, _ = _apply_journey(copy.deepcopy(en), timezone="America/Chicago")
    prompt = _prompt(chicago)
    assert "This business runs on America/Chicago time (UTC-6 standard, UTC-5 during daylight saving time)" in prompt
    assert "(for example America/New_York)" in prompt
    assert _RuntimeContract._STYLE_RELATIVE_DATE_LINE["en"] not in prompt

    new_york, _ = _apply_journey(copy.deepcopy(en), timezone="America/New_York")
    assert "(for example UTC)" in _prompt(new_york)


def test_a_changed_zone_replaces_the_block_instead_of_adding_one():
    seoul, _ = _apply_journey(_journey_flow())
    tokyo, notes = _apply_journey(copy.deepcopy(seoul), timezone="Asia/Tokyo")
    prompt = _prompt(tokyo)
    assert prompt.count("[date basis]") == 1 and "Asia/Tokyo" in prompt and "Asia/Seoul" not in prompt
    assert any("(J11)" in n for n in notes)


def test_a_fast_model_journey_that_captures_a_date_moves_to_the_tool_model():
    flow = _journey_flow(journey_cfg={"modelType": "anthropic.claude-haiku-4-5"})
    flow["slotTypes"].append({"name": "visitDate", "type": "NLX.Date", "sensitive": False})
    steps = [{"captures": ["reason", "visitDate"], "journey_tools": []}]
    out, notes = _apply_journey(copy.deepcopy(flow), journey_steps=steps)
    cfg = out["nodes"]["gj"]["metadata"]["generativeJourney"]
    assert cfg["modelType"] == _RuntimeContract.JOURNEY_TOOL_MODEL
    assert any("weekday wrong" in n for n in notes)
    # a journey that captures no date keeps its model
    plain, _ = _apply_journey(_journey_flow(journey_cfg={"modelType": "anthropic.claude-haiku-4-5"}))
    assert plain["nodes"]["gj"]["metadata"]["generativeJourney"]["modelType"] == "anthropic.claude-haiku-4-5"
    # without a known zone the old "ask for the month and day" rule stands, and so does the model
    en = copy.deepcopy(flow)
    en["mainLanguageCode"] = "en-US"
    out_en, _ = _apply_journey(en, journey_steps=steps)
    assert out_en["nodes"]["gj"]["metadata"]["generativeJourney"]["modelType"] == "anthropic.claude-haiku-4-5"


def test_zone_helpers():
    assert canonical_timezone("asia/seoul") == "Asia/Seoul"
    assert canonical_timezone("America/New York") == "America/New_York"
    assert canonical_timezone("KST") is None and canonical_timezone("") is None
    assert business_timezone(None, "ko-KR") == "Asia/Seoul"
    assert business_timezone("", "ja") == "Asia/Tokyo"
    assert business_timezone(None, "en-US") is None
    assert business_timezone("Europe/London", "ko-KR") == "Europe/London"
    assert business_timezone("Mars/Base", "ko-KR") == "Asia/Seoul"        # an unknown name is not trusted
    assert application_timezone({"application": {"primary_locale": "ko-KR"}}) == "Asia/Seoul"
    assert application_timezone({"application": {"locales": ["en-US"], "timezone": "America/Denver"}}) \
        == "America/Denver"
    assert application_timezone({"application": {}, "business_profile": {"language": "ja-JP"}}) == "Asia/Tokyo"
    assert application_timezone(None) is None
    assert offset_summary("Asia/Kolkata", "en") == "UTC+5:30, no daylight saving time"
    assert offset_summary("Europe/Berlin", "ko") == "표준시 UTC+1, 서머타임 기간 UTC+2"


def test_the_packaging_pass_uses_the_recorded_zone():
    from tools.validate_acxd_consistency import bundle_contract_kwargs
    flow = _journey_flow(journey_cfg=copy.deepcopy(_CARRYING))
    flow["mainLanguageCode"] = "en-US"
    bundle = {"flows": [flow]}
    kwargs = bundle_contract_kwargs(bundle, {"application": {"locales": ["en-US"], "timezone": "America/Chicago"}})
    assert kwargs["kwargs"]["timezone"] == "America/Chicago"
    out, _ = apply_runtime_contract(copy.deepcopy(flow),
                                    **{**_journey_kwargs(), "timezone": kwargs["kwargs"]["timezone"]})
    assert "America/Chicago" in _prompt(out)


def test_the_backend_contract_names_the_zone_dates_are_in():
    from tools.asset_packager import _generate_backend_contract
    with_zone = _generate_backend_contract({"data_requests": []}, {"auth_header": None, "timezone": "Asia/Seoul"})
    assert "5. **Dates are calendar dates in `Asia/Seoul`.**" in with_zone
    assert "America/New_York" in with_zone and "same-day" in with_zone
    without = _generate_backend_contract({"data_requests": []}, {"auth_header": None})
    assert "Dates are calendar dates" not in without


def test_a_flow_without_a_language_code_is_judged_by_its_messages():
    """`_language()` tested the bound method `is_korean` (always truthy), so a
    flow with no mainLanguageCode got Korean rules even when every message was
    English."""
    flow = _journey_flow()
    flow.pop("mainLanguageCode", None)
    flow.pop("languageCodes", None)
    for node in flow["nodes"].values():
        for message in node.get("messages") or []:
            message["body"] = "Please tell me your order number."
    flow["nodes"]["gj"]["metadata"]["generativeJourney"]["prompt"] = "Confirm why the customer returns the item."
    out, _ = _apply_journey(flow)
    prompt = _prompt(out)
    assert "Do not echo each answer back" in prompt and "되풀이하지" not in prompt
    assert "[date basis]" not in prompt                                  # no language, no zone
