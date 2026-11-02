"""Bounded, read-only Open-Meteo city forecast. No request or response logging."""

import asyncio
import json
import math
import re
import time
import unicodedata
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from companion.tools.read_only import ReadOnlyTool

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
UNAVAILABLE = "天气查询暂不可用，请稍后再试；没有可用的天气数据。"
CLARIFY = "请提供更具体的城市名称，例如加上省份或国家，以便查询天气。"
UNKNOWN = "没有找到这个城市，请检查名称或补充省份、国家后再试。"
MAX_BODY = 256_000


def normalize(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def city_parts(value):
    if not isinstance(value, str) or len(value) > 80:
        return []
    if re.search(
        r"\b(ignore|instructions?|system|prompt|assistant|execute|override)\b|"
        r"忽略|指令|系统提示|执行",
        normalize(value),
    ):
        return []
    parts = [part.strip() for part in unicodedata.normalize("NFKC", value).split(",")]
    if not 1 <= len(parts) <= 3:
        return []
    for part in parts:
        if not 1 <= len(part) <= 40 or not any(c.isalpha() for c in part):
            return []
        if any(
            not (c.isalpha() or unicodedata.category(c).startswith("M") or c in " '-.")
            for c in part
        ):
            return []
    return parts


def user_provided(args, message):
    if not isinstance(message, str) or not city_parts(args.get("city")):
        return False
    city, text = normalize(args["city"]), normalize(message)
    # The complete city and qualifiers must occur in this turn, not just history.
    if city not in text:
        return False
    index = text.find(city)
    # Prevent short Latin names matching inside unrelated words.
    if city[0].isascii() and city[0].isalpha() and index and text[index - 1].isalpha():
        return False
    end = index + len(city)
    if city[-1].isascii() and city[-1].isalpha() and end < len(text) and text[end].isalpha():
        return False
    tomorrow = "明天" in text or "tomorrow" in text
    return (args["day"] == "tomorrow") == tomorrow


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _get_json(endpoint, params):
    # urllib does not emit httpx's INFO request-URL logs; redirects are refused.
    if endpoint not in (GEOCODING_URL, FORECAST_URL):
        raise ValueError("unsupported endpoint")
    request = Request(endpoint + "?" + urlencode(params), headers={"Accept": "application/json"})
    with build_opener(_NoRedirect()).open(request, timeout=3) as response:
        body = response.read(MAX_BODY + 1)
    if len(body) > MAX_BODY:
        raise ValueError("response too large")
    return json.loads(body)


async def fetch(endpoint, params):
    return await asyncio.to_thread(_get_json, endpoint, params)


def _number(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def _place_label(place):
    parts = []
    for key in ("name", "admin1", "country"):
        value = place.get(key)
        if value:
            if not isinstance(value, str) or len(value) > 40 or not city_parts(value):
                raise ValueError("invalid place label")
            if normalize(value) not in {normalize(p) for p in parts}:
                parts.append(value)
    if not parts:
        raise ValueError("missing name")
    return ", ".join(parts)


class WeatherAdapter:
    def __init__(self, *, get=fetch, now=None, monotonic=None):
        self.get = get
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self.cache = {}
        self.requests = deque()

    async def _request(self, endpoint, params):
        now = self.monotonic()
        while self.requests and self.requests[0] <= now - 86400:
            self.requests.popleft()
        if (
            len(self.requests) >= 5000
            or sum(stamp > now - 3600 for stamp in self.requests) >= 2000
            or sum(stamp > now - 60 for stamp in self.requests) >= 300
        ):
            raise ValueError("provider request budget exhausted")
        self.requests.append(now)
        return await self.get(endpoint, params)

    async def lookup(self, city, day):
        parts = city_parts(city)
        if not parts or day not in ("today", "tomorrow"):
            return CLARIFY
        try:
            # Resolve the date in the provider's timezone, not the server timezone.
            geo = await self._request(
                GEOCODING_URL,
                {
                    "name": parts[0],
                    "count": 100,
                    "language": "en" if parts[0].isascii() else "zh",
                    "format": "json",
                },
            )
            if not isinstance(geo, dict) or geo.get("error"):
                return UNAVAILABLE
            places = geo.get("results", [])
            if not isinstance(places, list) or any(not isinstance(p, dict) for p in places):
                return UNAVAILABLE
            matches = []
            for place in places:
                if not isinstance(place.get("name"), str) or normalize(place["name"]) != normalize(
                    parts[0]
                ):
                    continue
                qualifiers = [
                    normalize(str(place.get(k) or ""))
                    for k in ("admin1", "country", "country_code")
                ]
                if all(normalize(p) in qualifiers for p in parts[1:]):
                    matches.append(place)
            if not matches:
                return UNKNOWN
            if len(matches) != 1 or len(places) >= 100:
                return CLARIFY
            place = matches[0]
            label = _place_label(place)
            lat, lon, tz = place.get("latitude"), place.get("longitude"), place.get("timezone")
            if not _number(lat, -90, 90) or not _number(lon, -180, 180) or not isinstance(tz, str):
                return UNAVAILABLE
            zone = ZoneInfo(tz)
            local_date = self.now().astimezone(zone).date() + timedelta(days=day == "tomorrow")
            date = local_date.isoformat()
            key = (normalize(city), date)
            current = self.monotonic()
            self.cache = {k: v for k, v in self.cache.items() if current - v[0] < 600}
            if key in self.cache:
                return self.cache[key][1]
            forecast = await self._request(
                FORECAST_URL,
                {
                    "latitude": lat,
                    "longitude": lon,
                    "timezone": tz,
                    "start_date": date,
                    "end_date": date,
                    "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
                    "temperature_unit": "celsius",
                    "precipitation_unit": "mm",
                },
            )
            daily, units = forecast["daily"], forecast["daily_units"]
            if forecast.get("error") or forecast["timezone"] != tz or daily["time"] != [date]:
                return UNAVAILABLE
            values = []
            for field, unit, low, high in (
                ("temperature_2m_max", "°C", -100, 70),
                ("temperature_2m_min", "°C", -100, 70),
                ("precipitation_sum", "mm", 0, 5000),
            ):
                items = daily[field]
                if units[field] != unit or not isinstance(items, list) or len(items) != 1:
                    return UNAVAILABLE
                if not _number(items[0], low, high):
                    return UNAVAILABLE
                values.append(items[0])
            if values[1] > values[0]:
                return UNAVAILABLE
            result = (
                f"已向 Open-Meteo 外部查询。{label} {date} 全天预报"
                f"（预报时段起点 {date} 00:00，时区 {tz}）："
                f"最高温 {values[0]} °C，最低温 {values[1]} °C，降水量 {values[2]} mm。"
                "来源：Open-Meteo，CC BY 4.0（https://open-meteo.com/）。"
            )
            if len(self.cache) >= 128:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = (current, result)
            return result
        except (Exception, asyncio.TimeoutError, ZoneInfoNotFoundError):
            return UNAVAILABLE


def weather_tool(adapter=None):
    adapter = adapter or WeatherAdapter()
    return ReadOnlyTool(
        "weather_lookup",
        "仅在用户本次明确提供城市时查询天气；city 必须逐字来自当前消息，"
        "day 仅 today 或 tomorrow；有歧义时向用户澄清。查询会发送城市到 Open-Meteo。",
        {
            "type": "object",
            "properties": {
                "city": {"type": "string", "minLength": 1, "maxLength": 80},
                "day": {"type": "string", "maxLength": 8, "enum": ["today", "tomorrow"]},
            },
            "required": ["city", "day"],
            "additionalProperties": False,
        },
        adapter.lookup,
        user_provided,
    )
