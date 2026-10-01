"""Curated cities and a small, validated Aladhan prayer-time client."""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime
import threading
from zoneinfo import ZoneInfo

import httpx


PRAYER_KEYS = ("fajr", "dhuhr", "asr", "maghrib", "isha")

# IDs and coordinates are curated configuration. No account data is sent upstream.
CITIES = (
    {"id": "jeddah", "name": "جدة", "name_en": "Jeddah", "country": "Saudi Arabia", "timezone": "Asia/Riyadh", "method": 4, "latitude": 21.4858, "longitude": 39.1925},
    {"id": "makkah", "name": "مكة المكرمة", "name_en": "Makkah", "country": "Saudi Arabia", "timezone": "Asia/Riyadh", "method": 4, "latitude": 21.3891, "longitude": 39.8579},
    {"id": "riyadh", "name": "الرياض", "name_en": "Riyadh", "country": "Saudi Arabia", "timezone": "Asia/Riyadh", "method": 4, "latitude": 24.7136, "longitude": 46.6753},
    {"id": "medina", "name": "المدينة المنورة", "name_en": "Medina", "country": "Saudi Arabia", "timezone": "Asia/Riyadh", "method": 4, "latitude": 24.5247, "longitude": 39.5692},
    {"id": "alexandria", "name": "الإسكندرية", "name_en": "Alexandria", "country": "Egypt", "timezone": "Africa/Cairo", "method": 5, "latitude": 31.2001, "longitude": 29.9187},
    {"id": "cairo", "name": "القاهرة", "name_en": "Cairo", "country": "Egypt", "timezone": "Africa/Cairo", "method": 5, "latitude": 30.0444, "longitude": 31.2357},
)
CITIES_BY_ID = {city["id"]: city for city in CITIES}


class ScheduleUnavailable(Exception):
    pass


class ScheduleService:
    def __init__(self, *, timeout: float = 4.0, max_entries: int = 512):
        self.timeout = timeout
        self.max_entries = max_entries
        self._cache: OrderedDict[tuple[str, str], dict] = OrderedDict()
        self._lock = threading.Lock()

    def now(self, city: dict) -> datetime:
        return datetime.now(ZoneInfo(city["timezone"]))

    def today(self, city: dict) -> str:
        return self.now(city).date().isoformat()

    def timings(self, city: dict, day: str) -> dict[str, str]:
        key = (city["id"], day)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached.copy()

        display_date = date.fromisoformat(day).strftime("%d-%m-%Y")
        try:
            response = httpx.get(
                f"https://api.aladhan.com/v1/timings/{display_date}",
                params={"latitude": city["latitude"], "longitude": city["longitude"],
                        "method": city["method"], "iso8601": "true",
                        "timezonestring": city["timezone"]},
                timeout=self.timeout,
            )
            response.raise_for_status()
            raw = response.json()["data"]["timings"]
            result = {}
            parsed_times = []
            for key_name, provider_name in zip(PRAYER_KEYS, ("Fajr", "Dhuhr", "Asr", "Maghrib", "Isha")):
                value = raw[provider_name]
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None or parsed.date().isoformat() != day:
                    raise ValueError("invalid prayer timestamp")
                expected_offset = parsed.astimezone(ZoneInfo(city["timezone"])).utcoffset()
                if parsed.utcoffset() != expected_offset:
                    raise ValueError("invalid prayer timezone")
                parsed_times.append(parsed)
                result[key_name] = parsed.isoformat()
            if parsed_times != sorted(parsed_times) or len(set(parsed_times)) != len(parsed_times):
                raise ValueError("invalid prayer ordering")
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise ScheduleUnavailable("تعذر تحميل مواقيت الصلاة الآن") from exc

        with self._lock:
            self._cache[key] = result.copy()
            self._cache.move_to_end(key)
            while len(self._cache) > self.max_entries:
                self._cache.popitem(last=False)
        return result

    def schedule(self, city: dict, day: str) -> dict:
        today = self.today(city)
        try:
            times = self.timings(city, day)
        except ScheduleUnavailable as exc:
            return {"available": False, "timezone": city["timezone"], "today": today,
                    "times": None, "eligible_prayers": None, "error": str(exc)}
        if day < today:
            eligible = list(PRAYER_KEYS)
        elif day > today:
            eligible = []
        else:
            now = self.now(city)
            eligible = [key for key in PRAYER_KEYS if datetime.fromisoformat(times[key]) <= now]
        return {"available": True, "timezone": city["timezone"], "today": today,
                "times": times, "eligible_prayers": eligible, "source": "Aladhan"}
