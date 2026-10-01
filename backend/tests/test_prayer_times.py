import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx

from backend.main import create_app
from backend.schedule import CITIES_BY_ID, ScheduleService


class FixedSchedules:
    """Deterministic clock/provider seam for API integration tests."""

    def __init__(self, *, now=None, available=True):
        self.current = now or datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
        self.available = available
        self.calls = []

    def today(self, city):
        return self.current.astimezone(ZoneInfo(city["timezone"])).date().isoformat()

    def schedule(self, city, day):
        self.calls.append((city["id"], day))
        today = self.today(city)
        if not self.available:
            return {"available": False, "timezone": city["timezone"], "today": today,
                    "times": None, "eligible_prayers": None, "error": "provider unavailable"}
        if day < today:
            eligible = ["fajr", "dhuhr", "asr", "maghrib", "isha"]
        elif day > today:
            eligible = []
        else:
            eligible = ["fajr", "dhuhr"]
        return {"available": True, "timezone": city["timezone"], "today": today,
                "times": {key: f"{day}T{hour}:00:00+03:00" for key, hour in
                          (("fajr", "05"), ("dhuhr", "12"), ("asr", "15"),
                           ("maghrib", "18"), ("isha", "19"))},
                "eligible_prayers": eligible, "source": "test"}


class PrayerTimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.schedules = FixedSchedules()
        self.app = create_app(Path(self.temp.name) / "test.sqlite3", schedule_service=self.schedules)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                         base_url="http://testserver",
                                         headers={"Origin": "http://testserver"})

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def register(self, client=None, alias="city-star"):
        response = await (client or self.client).post(
            "/api/v1/auth/register", json={"alias": alias, "password": "long-test-password"})
        self.assertEqual(response.status_code, 201, response.text)

    async def choose_jeddah(self, client=None):
        response = await (client or self.client).patch("/api/v1/account", json={"city_id": "jeddah"})
        self.assertEqual(response.status_code, 200, response.text)

    async def test_current_day_uses_elapsed_prayers_and_gates_future_mutations(self):
        await self.register()
        await self.choose_jeddah()
        day = "/api/v1/days/2026-10-01"
        snapshot = (await self.client.get(day)).json()
        self.assertEqual(snapshot["schedule"]["eligible_prayers"], ["fajr", "dhuhr"])
        self.assertEqual(snapshot["stats"]["total"], 2)
        self.assertEqual(snapshot["stats"]["gold_target"], 54)
        self.assertEqual(snapshot["stats"]["silver_target"], 4)
        self.assertEqual(snapshot["stats"]["percent"], 0)

        await self.client.patch(f"{day}/prayers/fajr", json={"state": 2})
        completed = (await self.client.patch(f"{day}/prayers/dhuhr", json={"state": 1})).json()
        self.assertEqual(completed["stats"]["completed"], 2)
        self.assertEqual(completed["stats"]["percent"], 100)
        self.assertEqual(completed["stats"]["gold_medals"], 28)
        self.assertEqual((await self.client.patch(f"{day}/prayers/asr", json={"state": 1})).status_code, 409)

        payload = {key: completed[key] for key in ("prayers", "sunnah", "dhuhr_before_blocks", "version")}
        payload["prayers"]["asr"] = 1
        self.assertEqual((await self.client.put(day, json=payload)).status_code, 409)

        aggregate = (await self.client.get("/api/v1/statistics")).json()
        self.assertEqual(aggregate["total"], 2)
        self.assertEqual(aggregate["percent"], 100)
        self.assertEqual(aggregate["gold_target"], 54)
        self.assertEqual(aggregate["silver_target"], 4)

    async def test_historical_day_keeps_full_targets_and_provider_failure_is_explicit(self):
        await self.register()
        await self.choose_jeddah()
        yesterday = (await self.client.get("/api/v1/days/2026-09-30")).json()
        self.assertEqual(yesterday["stats"]["total"], 5)
        self.assertEqual(yesterday["stats"]["gold_target"], 135)
        self.assertEqual(yesterday["stats"]["silver_target"], 7)
        self.assertEqual(yesterday["schedule"]["eligible_prayers"], ["fajr", "dhuhr", "asr", "maghrib", "isha"])

        self.schedules.available = False
        current = (await self.client.get("/api/v1/days/2026-10-01")).json()
        self.assertIsNone(current["stats"]["percent"])
        self.assertIsNone(current["stats"]["gold_target"])
        self.assertEqual((await self.client.patch("/api/v1/days/2026-10-01/prayers/fajr",
                                                  json={"state": 1})).status_code, 503)
        historical = (await self.client.get("/api/v1/days/2026-09-30")).json()
        self.assertEqual(historical["stats"]["total"], 5)

    async def test_city_catalog_and_account_city_are_persisted_and_isolated(self):
        await self.register()
        cities = (await self.client.get("/api/v1/cities")).json()["cities"]
        jeddah = next(city for city in cities if city["id"] == "jeddah")
        self.assertEqual(jeddah["timezone"], "Asia/Riyadh")
        self.assertEqual((await self.client.patch("/api/v1/account", json={"city_id": "unknown"})).status_code, 422)
        self.assertEqual((await self.client.patch("/api/v1/account", json={"city_id": "jeddah"})).json()["user"]["city_id"], "jeddah")

        other = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver",
                                  headers={"Origin": "http://testserver"})
        try:
            await self.register(other, alias="other-city-star")
            self.assertIsNone((await other.get("/api/v1/auth/me")).json()["user"].get("city_id"))
            blank = (await other.get("/api/v1/days/2026-10-01")).json()
            self.assertNotIn("schedule", blank)
            self.assertEqual(blank["stats"]["total"], 5)
        finally:
            await other.aclose()


class ScheduleServiceTests(unittest.TestCase):
    def test_provider_failure_and_city_timezone_data_are_safe(self):
        self.assertEqual(CITIES_BY_ID["alexandria"]["timezone"], "Africa/Cairo")
        self.assertEqual(CITIES_BY_ID["jeddah"]["timezone"], "Asia/Riyadh")

        service = ScheduleService(timeout=0.01)
        with patch("backend.schedule.httpx.get", side_effect=httpx.HTTPError("offline")):
            result = service.schedule(CITIES_BY_ID["jeddah"], "2026-10-01")
        self.assertFalse(result["available"])
        self.assertIsNone(result["times"])
        self.assertIsNone(result["eligible_prayers"])


if __name__ == "__main__":
    unittest.main()
