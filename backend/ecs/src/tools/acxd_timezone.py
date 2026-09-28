"""The business time zone an ACXD application's journeys count dates in.

Live (Workshop Studio sandbox, 2026-09-28): the ACXD runtime runs every
conversation on ``America/New_York`` whatever the caller's or the workspace's
location. A debug message printed ``{System.timezone:NLX.System}`` as
``America/New_York`` while the browser was on Asia/Seoul, and a generative
journey asked to repeat the clock it was given answered "Now it is Sunday,
2026-09-27 8:28 PM (America/New_York)" at 09:28 on Monday in Seoul. Neither the
application settings, the workspace settings, the test panel nor the SDK
(``ApplicationSettings`` / ``UpdateWorkspaceRequest``) has a time-zone field, so
for a Korean caller "내일" meant the wrong day from 00:00 to 13:00 (14:00 in the
US winter) every day.

A journey told which zone the business runs on converts the clock itself and
counts from that date (the same sandbox: "오늘 2026-09-28 (월요일), Asia/Seoul",
and "내일 오전 10시" booked 2026-09-29). This module names that zone: the one the
interview recorded, else the one the application's language implies (Korea and
Japan each have a single zone without daylight saving time). Any other language
names no single zone, so the interview records it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone as dt_timezone
from functools import lru_cache
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: The zone a primary language implies when the interview recorded none.
DEFAULT_TIMEZONE_BY_LANGUAGE = {"ko": "Asia/Seoul", "ja": "Asia/Tokyo"}

#: The zone the ACXD runtime reports for every conversation (live, 2026-09-28).
RUNTIME_TIMEZONE = "America/New_York"

#: Offsets for the language defaults, used when the tz database is unavailable.
_STATIC_OFFSETS = {"Asia/Seoul": (timedelta(hours=9), timedelta(hours=9)),
                   "Asia/Tokyo": (timedelta(hours=9), timedelta(hours=9)),
                   "UTC": (timedelta(0), timedelta(0))}


@lru_cache(maxsize=1)
def _known_zones() -> dict[str, str]:
    """lower-case name -> canonical IANA name, from the tz database."""
    try:
        import zoneinfo
        zones = zoneinfo.available_timezones()
    except Exception as exc:  # pragma: no cover - the image ships tzdata
        logger.warning("[ACXD timezone] tz database unavailable: %s", exc)
        zones = set(_STATIC_OFFSETS)
    return {name.lower(): name for name in zones} | {"utc": "UTC"}


def canonical_timezone(value: Any) -> Optional[str]:
    """The IANA name for ``value`` ('asia/seoul' -> 'Asia/Seoul'), or None."""
    raw = str(value or "").strip().replace(" ", "_")
    if not raw:
        return None
    return _known_zones().get(raw.lower())


def business_timezone(explicit: Any, language_code: Any) -> Optional[str]:
    """The zone the application's journeys count dates in: the recorded one,
    else the one its primary language implies, else None (unknown)."""
    recorded = canonical_timezone(explicit)
    if recorded:
        return recorded
    code = str(language_code or "").strip().lower()
    for prefix, zone in DEFAULT_TIMEZONE_BY_LANGUAGE.items():
        if code == prefix or code.startswith(prefix + "-") or code.startswith(prefix + "_"):
            return zone
    return None


def application_timezone(spec: Any) -> Optional[str]:
    """``business_timezone`` for a generation context / ACXDFlowSpec dict."""
    if not isinstance(spec, dict):
        return None
    app = spec.get("application") if isinstance(spec.get("application"), dict) else {}
    locales = app.get("locales") if isinstance(app.get("locales"), list) else []
    language = (app.get("primary_locale") or app.get("primary_language")
                or (locales[0] if locales else None))
    if not language:
        profile = spec.get("business_profile") if isinstance(spec.get("business_profile"), dict) else {}
        language = profile.get("language") or profile.get("primary_language")
    return business_timezone(app.get("timezone"), language)


def _offsets(zone_name: str) -> Optional[tuple[timedelta, timedelta]]:
    """(standard, daylight) UTC offsets of the zone this year."""
    try:
        import zoneinfo
        zone = zoneinfo.ZoneInfo(zone_name)
    except Exception:
        return _STATIC_OFFSETS.get(zone_name)
    year = datetime.now(dt_timezone.utc).year
    samples = [datetime(year, month, 15, 12, tzinfo=zone).utcoffset() for month in (1, 7)]
    samples = [s for s in samples if s is not None]
    if not samples:
        return None
    return min(samples), max(samples)


def _utc(offset: timedelta) -> str:
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    hours, rest = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours}" + (f":{rest:02d}" if rest else "")


def offset_summary(zone_name: str, lang: str) -> str:
    """'UTC+9, 서머타임 없음' / 'UTC-5 standard, UTC-4 during daylight saving time'."""
    offsets = _offsets(zone_name)
    if offsets is None:
        return ""
    standard, daylight = offsets
    if standard == daylight:
        return {"ko": f"{_utc(standard)}, 서머타임 없음",
                "ja": f"{_utc(standard)}、夏時間なし"}.get(lang, f"{_utc(standard)}, no daylight saving time")
    return {"ko": f"표준시 {_utc(standard)}, 서머타임 기간 {_utc(daylight)}",
            "ja": f"標準時 {_utc(standard)}、夏時間 {_utc(daylight)}"}.get(
        lang, f"{_utc(standard)} standard, {_utc(daylight)} during daylight saving time")


def runtime_clock_example(zone_name: str) -> str:
    """A zone the runtime's clock may be in that is not the business's own."""
    return "UTC" if zone_name == RUNTIME_TIMEZONE else RUNTIME_TIMEZONE
