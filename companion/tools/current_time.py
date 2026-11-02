"""Local system-clock time in the explicitly declared Shanghai timezone."""

import json
import re
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from companion.tools.read_only import ReadOnlyTool, _unique_object

ZONE = "Asia/Shanghai"
UNAVAILABLE_REPLY = "当前时间暂不可用，请稍后再试。"
CLARIFY_REPLY = "请确认是否要查询 Asia/Shanghai 的当前时间。"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
KEYS = ("date", "weekday", "time", "time_of_day", "timezone", "utc_offset")
SCHEMA = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
# Full-message matches deliberately avoid inferring intent, location or a requested timezone.
_REQUEST = re.compile(
    r"(?:现在几点(?:了|钟)?|现在是什么时间|现在的时间|当前时间|"
    r"what time is it(?: now)?|(?:what is |what's )the current time|current time)"
    r"[?？!！。.]?", re.IGNORECASE
)

_AMBIGUOUS_REQUEST = re.compile(
    r"(?:几点|what time|现在几点在[^?？!！。.]{1,40}|"
    r"current time in [^?？!！。.]{1,40})[?？!！。.]?",
    re.IGNORECASE,
)


def is_ambiguous_time_request(message):
    return isinstance(message, str) and _AMBIGUOUS_REQUEST.fullmatch(message.strip()) is not None


def is_current_time_request(message):
    return isinstance(message, str) and _REQUEST.fullmatch(message.strip()) is not None


def _period(hour):
    return ("night", "morning", "afternoon", "evening")[hour // 6]


def _system_clock():
    return datetime.now(timezone.utc)


def current_time_tool(clock=None, zone_factory=None):
    clock = clock or _system_clock
    zone_factory = zone_factory or ZoneInfo

    async def action():
        instant = clock()
        if not isinstance(instant, datetime) or instant.utcoffset() != timezone.utc.utcoffset(None):
            raise ValueError("invalid clock")
        zone = zone_factory(ZONE)
        if not isinstance(zone, ZoneInfo) or zone.key != ZONE:
            raise ValueError("invalid timezone")
        local = instant.astimezone(zone)
        offset = local.utcoffset()
        if offset is None or offset.total_seconds() % 60:
            raise ValueError("invalid timezone offset")
        minutes = int(offset.total_seconds() // 60)
        if abs(minutes) >= 24 * 60:
            raise ValueError("invalid timezone offset")
        sign = "+" if minutes >= 0 else "-"
        hours, mins = divmod(abs(minutes), 60)
        data = {
            "date": f"{local.year:04d}-{local.month:02d}-{local.day:02d}",
            "weekday": WEEKDAYS[local.weekday()],
            "time": f"{local.hour:02d}:{local.minute:02d}:{local.second:02d}",
            "time_of_day": _period(local.hour),
            "timezone": ZONE,
            "utc_offset": f"{sign}{hours:02d}:{mins:02d}",
        }
        return json.dumps(data, ensure_ascii=True, separators=(",", ":"))

    return ReadOnlyTool("current_time", "Get the current time in Asia/Shanghai.", SCHEMA, action,
                        user_input_check=lambda _args, message: is_current_time_request(message))


def format_current_time(result):
    """Render only a complete, internally consistent gateway result."""
    try:
        wrapper = json.loads(result, object_pairs_hook=_unique_object)
        if not isinstance(wrapper, dict) or set(wrapper) != {"status", "data"}:
            return UNAVAILABLE_REPLY
        if wrapper["status"] != "ok" or not isinstance(wrapper["data"], str):
            return UNAVAILABLE_REPLY
        data = json.loads(wrapper["data"], object_pairs_hook=_unique_object)
        if not isinstance(data, dict) or tuple(data) != KEYS:
            return UNAVAILABLE_REPLY
        if not all(isinstance(value, str) for value in data.values()):
            return UNAVAILABLE_REPLY
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", data["date"], re.ASCII):
            return UNAVAILABLE_REPLY
        if not re.fullmatch(r"\d{2}:\d{2}:\d{2}", data["time"], re.ASCII):
            return UNAVAILABLE_REPLY
        day = date.fromisoformat(data["date"])
        clock_time = time.fromisoformat(data["time"])
        offset = re.fullmatch(r"[+-](\d{2}):(\d{2})", data["utc_offset"], re.ASCII)
        if (data["weekday"] != WEEKDAYS[day.weekday()]
                or data["time_of_day"] != _period(clock_time.hour)
                or data["timezone"] != ZONE or offset is None
                or int(offset[1]) >= 24 or int(offset[2]) >= 60):
            return UNAVAILABLE_REPLY
        return (f'{data["date"]} {data["weekday"]} {data["time"]} '
                f'({data["time_of_day"]}, {data["timezone"]}, UTC{data["utc_offset"]})')
    except (ValueError, TypeError, KeyError, RecursionError):
        return UNAVAILABLE_REPLY
