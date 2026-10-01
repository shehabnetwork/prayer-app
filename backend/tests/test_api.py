import asyncio
import tempfile
import secrets
import unittest
from pathlib import Path

import httpx

from backend.main import COOKIE_NAME, create_app, token_hash


class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Hashing in synchronous app construction is expected, not a stuck async task.
        asyncio.get_running_loop().slow_callback_duration = 1.0
        self.password = secrets.token_urlsafe(24)
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite3"
        self.app = create_app(self.path)
        self.client = self.new_client()
        self.clients = [self.client]
        self.day = "/api/v1/days/2026-09-30"

    async def asyncTearDown(self):
        for client in self.clients:
            await client.aclose()
        self.temp.cleanup()

    def new_client(self, app=None, origin=True):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app or self.app),
                                 base_url="http://testserver",
                                 headers={"Origin": "http://testserver"} if origin else {})

    async def register(self, client=None, alias="نجم تجريبي"):
        client = client or self.client
        response = await client.post("/api/v1/auth/register", json={"alias": alias, "password": self.password})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def test_registration_cookie_hashing_and_me(self):
        auth = await self.register()
        self.assertEqual(auth["user"]["alias"], "نجم تجريبي")
        response = await self.client.get("/api/v1/auth/me")
        self.assertEqual(response.json(), {"user": auth["user"]})
        cookies = self.client.cookies.get(COOKIE_NAME)
        self.assertEqual(cookies, auth["access_token"])
        with self.app.state.database.connect() as conn:
            user = conn.execute("SELECT * FROM users").fetchone()
            session = conn.execute("SELECT * FROM sessions").fetchone()
        self.assertNotIn(self.password, user["password_hash"])
        self.assertEqual(session["token_hash"], token_hash(auth["access_token"]))
        self.assertNotEqual(session["token_hash"], auth["access_token"])
        response = await self.client.post("/api/v1/auth/login", json={"alias": "نجم تجريبي", "password": self.password})
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=lax", cookie)
        self.assertIn("path=/", cookie)

    async def test_alias_validation_password_validation_and_duplicates(self):
        for payload in (
            {"alias": "child@example.com", "password": self.password},
            {"alias": "xx", "password": self.password},
            {"alias": "sample-star", "password": "short"},
            {"alias": "sample-star", "password": "        "},
            {"alias": "sample-star", "password": self.password, "email": "not-needed"},
        ):
            response = await self.client.post("/api/v1/auth/register", json=payload)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertNotIn(payload["password"], response.text)
        await self.register(alias="Sample-Star")
        response = await self.client.post("/api/v1/auth/register", json={"alias": "sample-star", "password": self.password})
        self.assertEqual(response.status_code, 409)

    async def test_login_invalid_credentials_casefold_and_bearer(self):
        await self.register(alias="Sample-Star")
        for alias, password in (("Sample-Star", "wrong-password"), ("Unknown-Star", "wrong-password")):
            response = await self.client.post("/api/v1/auth/login", json={"alias": alias, "password": password})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["detail"], "الاسم المستعار أو كلمة المرور غير صحيحة")
        response = await self.client.post("/api/v1/auth/login", json={"alias": "sample-star", "password": self.password})
        self.assertEqual(response.status_code, 200)
        token = response.json()["access_token"]
        mobile = self.new_client(origin=False)
        self.clients.append(mobile)
        response = await mobile.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["alias"], "Sample-Star")

    async def test_auth_required_and_expired_session(self):
        for path in (self.day, "/api/v1/history?from=2026-09-01&to=2026-09-30", "/api/v1/statistics", "/api/v1/auth/me"):
            self.assertEqual((await self.client.get(path)).status_code, 401)
        auth = await self.register()
        with self.app.state.database.connect() as conn:
            conn.execute("UPDATE sessions SET expires_at=0 WHERE token_hash=?", (token_hash(auth["access_token"]),))
        self.assertEqual((await self.client.get(self.day)).status_code, 401)

    async def test_per_user_isolation_for_days_history_statistics(self):
        await self.register()
        await self.client.post(f"{self.day}/prayers/fajr/cycle")
        other = self.new_client()
        self.clients.append(other)
        await self.register(other, "قمر تجريبي")
        response = await other.get(self.day)
        self.assertEqual(response.json()["prayers"]["fajr"], 0)
        self.assertEqual(response.json()["version"], 0)
        response = await other.get("/api/v1/history?from=2026-09-01&to=2026-09-30")
        self.assertEqual(response.json()["days"], [])
        self.assertEqual((await other.get("/api/v1/statistics")).json()["recorded_days"], 0)
        await other.patch(f"{self.day}/prayers/isha", json={"state": 2})
        self.assertEqual((await self.client.get(self.day)).json()["prayers"]["isha"], 0)

    async def test_logout_revokes_cookie_and_same_bearer(self):
        auth = await self.register()
        response = await self.client.post("/api/v1/auth/logout")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.assertIsNone(self.client.cookies.get(COOKIE_NAME))
        self.assertEqual((await self.client.get("/api/v1/auth/me")).status_code, 401)
        self.assertEqual((await self.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {auth['access_token']}"})).status_code, 401)

    async def test_persistence_after_new_app_instance(self):
        auth = await self.register()
        await self.client.patch(f"{self.day}/prayers/fajr", json={"state": 2})
        await self.client.patch(f"{self.day}/sunnah", json={"key": "dhuhr_before", "block": 1, "value": True})
        restarted = self.new_client(create_app(self.path), origin=False)
        self.clients.append(restarted)
        response = await restarted.get(self.day, headers={"Authorization": f"Bearer {auth['access_token']}"})
        self.assertEqual(response.status_code, 200)
        record = response.json()
        self.assertEqual(record["prayers"]["fajr"], 2)
        self.assertEqual(record["dhuhr_before_blocks"], [False, True])
        self.assertEqual(record["sunnah"]["dhuhr_before"], 2)
        self.assertEqual(record["version"], 2)

    async def test_cycle_and_five_prayer_denominator(self):
        await self.register()
        blank = (await self.client.get(self.day)).json()
        self.assertEqual(blank["stats"], {"completed": 0, "total": 5, "home": 0, "mosque": 0,
                                          "sunnah_rakahs": 0, "witr": False,
                                          "gold_medals": 0, "silver_medals": 0, "percent": 0})
        self.assertEqual(blank["dhuhr_before_blocks"], [False, False])
        for version, state in enumerate((1, 2, 0), start=1):
            record = (await self.client.post(f"{self.day}/prayers/fajr/cycle")).json()
            self.assertEqual(record["prayers"]["fajr"], state)
            self.assertEqual(record["version"], version)
            self.assertEqual(record["stats"]["total"], 5)
            self.assertEqual(record["stats"]["percent"], 20 if state else 0)
        for key in ("fajr_before", "dhuhr_after", "maghrib_after", "isha_after"):
            await self.client.patch(f"{self.day}/sunnah", json={"key": key, "value": 2})
        record = (await self.client.patch(f"{self.day}/sunnah", json={"key": "witr", "value": True})).json()
        self.assertEqual(record["stats"]["completed"], 0)
        self.assertEqual(record["stats"]["total"], 5)
        self.assertEqual(record["stats"]["sunnah_rakahs"], 8)
        self.assertTrue(record["stats"]["witr"])

    async def test_daily_medal_totals_cover_all_controls_and_reversals(self):
        await self.register()
        day = (await self.client.get(self.day)).json()
        self.assertEqual({key: day["stats"][key] for key in ("gold_medals", "silver_medals")},
                         {"gold_medals": 0, "silver_medals": 0})

        # Home is one gold medal and mosque is twenty-seven; state zero earns none.
        await self.client.patch(f"{self.day}/prayers/fajr", json={"state": 1})
        await self.client.patch(f"{self.day}/prayers/dhuhr", json={"state": 2})
        self.assertEqual((await self.client.get(self.day)).json()["stats"]["gold_medals"], 28)
        await self.client.patch(f"{self.day}/prayers/fajr", json={"state": 0})
        self.assertEqual((await self.client.get(self.day)).json()["stats"]["gold_medals"], 27)

        # Four single sunnah controls, two independent Dhuhr-before blocks, and Witr.
        for key in ("fajr_before", "dhuhr_after", "maghrib_after", "isha_after"):
            await self.client.patch(f"{self.day}/sunnah", json={"key": key, "value": 2})
        await self.client.patch(f"{self.day}/sunnah", json={"key": "dhuhr_before", "block": 0, "value": True})
        await self.client.patch(f"{self.day}/sunnah", json={"key": "dhuhr_before", "block": 1, "value": True})
        response = await self.client.patch(f"{self.day}/sunnah", json={"key": "witr", "value": True})
        self.assertEqual(response.json()["stats"]["silver_medals"], 7)

        # Repeating the same value is idempotent, and reversing each source removes exactly one medal.
        response = await self.client.patch(f"{self.day}/sunnah", json={"key": "witr", "value": True})
        self.assertEqual(response.json()["stats"]["silver_medals"], 7)
        await self.client.patch(f"{self.day}/sunnah", json={"key": "witr", "value": False})
        await self.client.patch(f"{self.day}/sunnah", json={"key": "dhuhr_before", "block": 0, "value": False})
        current = (await self.client.get(self.day)).json()
        self.assertEqual(current["stats"]["silver_medals"], 5)
        self.assertEqual(current["stats"]["gold_medals"], 27)

    async def test_medals_persist_in_reload_history_and_date_scoped_statistics(self):
        auth = await self.register()
        other_day = "/api/v1/days/2026-09-29"
        await self.client.patch(f"{self.day}/prayers/fajr", json={"state": 1})
        await self.client.patch(f"{self.day}/sunnah", json={"key": "fajr_before", "value": 2})
        await self.client.patch(f"{other_day}/prayers/isha", json={"state": 2})
        await self.client.patch(f"{other_day}/sunnah", json={"key": "witr", "value": True})

        restarted = self.new_client(create_app(self.path), origin=False)
        self.clients.append(restarted)
        day = (await restarted.get(self.day, headers={"Authorization": f"Bearer {auth['access_token']}"})).json()
        self.assertEqual((day["stats"]["gold_medals"], day["stats"]["silver_medals"]), (1, 1))
        history = (await restarted.get("/api/v1/history?from=2026-09-29&to=2026-09-30",
                                       headers={"Authorization": f"Bearer {auth['access_token']}"})).json()
        self.assertEqual([(item["date"], item["stats"]["gold_medals"], item["stats"]["silver_medals"])
                          for item in history["days"]], [("2026-09-29", 27, 1), ("2026-09-30", 1, 1)])
        stats = (await restarted.get("/api/v1/statistics?from=2026-09-29&to=2026-09-29",
                                     headers={"Authorization": f"Bearer {auth['access_token']}"})).json()
        self.assertEqual((stats["gold_medals"], stats["silver_medals"]), (27, 1))

    async def test_medals_are_isolated_between_accounts(self):
        await self.register()
        await self.client.patch(f"{self.day}/prayers/dhuhr", json={"state": 2})
        await self.client.patch(f"{self.day}/sunnah", json={"key": "witr", "value": True})
        other = self.new_client()
        self.clients.append(other)
        await self.register(other, "قمر تجريبي")
        stats = (await other.get("/api/v1/statistics")).json()
        self.assertEqual((stats["gold_medals"], stats["silver_medals"], stats["recorded_days"]), (0, 0, 0))

    async def test_independent_dhuhr_before_blocks(self):
        await self.register()
        for block, value, expected in ((1, True, [False, True]), (0, True, [True, True]),
                                        (1, False, [True, False]), (0, False, [False, False])):
            response = await self.client.patch(f"{self.day}/sunnah", json={"key": "dhuhr_before", "block": block, "value": value})
            self.assertEqual(response.status_code, 200, response.text)
            record = response.json()
            self.assertEqual(record["dhuhr_before_blocks"], expected)
            self.assertEqual(record["sunnah"]["dhuhr_before"], 2 * sum(expected))
            self.assertEqual(record["stats"]["sunnah_rakahs"], 2 * sum(expected))

    async def test_concurrent_cycles_and_blocks_do_not_lose_updates(self):
        await self.register()
        responses = await asyncio.gather(*(
            self.client.post(f"{self.day}/prayers/fajr/cycle") for _ in range(18)
        ))
        self.assertTrue(all(response.status_code == 200 for response in responses))
        record = (await self.client.get(self.day)).json()
        self.assertEqual(record["prayers"]["fajr"], 0)
        self.assertEqual(record["version"], 18)
        responses = await asyncio.gather(*(
            self.client.patch(f"{self.day}/sunnah", json={"key": "dhuhr_before", "block": block, "value": True})
            for block in (0, 1)
        ))
        self.assertTrue(all(response.status_code == 200 for response in responses))
        record = (await self.client.get(self.day)).json()
        self.assertEqual(record["dhuhr_before_blocks"], [True, True])
        self.assertEqual(record["sunnah"]["dhuhr_before"], 4)
        self.assertEqual(record["version"], 20)

    async def test_static_frontend_and_health_do_not_expose_database(self):
        self.assertEqual((await self.client.get("/")).status_code, 200)
        response = await self.client.get("/app.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        self.assertEqual((await self.client.get("/api/v1/health")).json(), {"ok": True})
        self.assertEqual((await self.client.get("/backend/data/prayer.sqlite3")).status_code, 404)

    async def test_strict_sunnah_and_prayer_validation(self):
        await self.register()
        invalid = (
            {"key": "fajr_before", "value": 4}, {"key": "fajr_before", "value": True},
            {"key": "fajr_before", "value": "2"}, {"key": "witr", "value": 1},
            {"key": "witr", "value": "true"}, {"key": "dhuhr_before", "value": 2},
            {"key": "dhuhr_before", "block": 2, "value": True},
            {"key": "dhuhr_before", "block": True, "value": True},
            {"key": "dhuhr_before", "block": 1, "value": 1},
            {"key": "fajr_before", "block": 0, "value": 2}, {"key": "unknown", "value": 2},
        )
        for payload in invalid:
            response = await self.client.patch(f"{self.day}/sunnah", json=payload)
            self.assertEqual(response.status_code, 422, response.text)
        for state in (3, -1, True, "1", None):
            self.assertEqual((await self.client.patch(f"{self.day}/prayers/fajr", json={"state": state})).status_code, 422)
        self.assertEqual((await self.client.post(f"{self.day}/prayers/unknown/cycle")).status_code, 422)
        self.assertEqual((await self.client.get(self.day)).json()["version"], 0)

    async def test_put_and_optimistic_concurrency(self):
        await self.register()
        day = (await self.client.get(self.day)).json()
        payload = {key: day[key] for key in ("prayers", "sunnah", "dhuhr_before_blocks", "version")}
        payload["prayers"]["fajr"] = 1
        response = await self.client.put(self.day, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["version"], 1)
        self.assertEqual((await self.client.put(self.day, json=payload)).status_code, 409)
        self.assertEqual((await self.client.post(f"{self.day}/prayers/fajr/cycle", json={"version": 0})).status_code, 409)
        self.assertEqual((await self.client.patch(f"{self.day}/sunnah", json={"key": "witr", "value": True, "version": 0})).status_code, 409)
        self.assertEqual((await self.client.patch(f"{self.day}/prayers/fajr", json={"state": 2, "version": 1})).status_code, 200)
        payload["version"] = 2
        payload["sunnah"]["dhuhr_before"] = 2
        self.assertEqual((await self.client.put(self.day, json=payload)).status_code, 422)

    async def test_date_validation_history_bounds_and_empty_reads(self):
        await self.register()
        for day in ("2026-02-30", "2026-9-30", "20260930", "2026-13-01", "0000-01-01"):
            self.assertEqual((await self.client.get(f"/api/v1/days/{day}")).status_code, 422)
        self.assertEqual((await self.client.get("/api/v1/days/2024-02-29")).status_code, 200)
        self.assertEqual((await self.client.get("/api/v1/statistics")).json()["recorded_days"], 0)
        for query in ("", "?from=2026-01-01", "?from=2026-10-01&to=2026-09-01",
                      "?from=2026-01-01&to=2027-01-02", "?from=bad&to=2026-09-30"):
            self.assertEqual((await self.client.get("/api/v1/history" + query)).status_code, 422)
        self.assertEqual((await self.client.get("/api/v1/history?from=2026-01-01&to=2027-01-01")).status_code, 200)
        await self.client.post(f"{self.day}/prayers/fajr/cycle")
        result = (await self.client.get("/api/v1/history?from=2026-09-01&to=2026-09-30")).json()
        self.assertEqual([day["date"] for day in result["days"]], ["2026-09-30"])
        self.assertEqual((await self.client.get("/api/v1/statistics?from=2026-10-01&to=2026-10-31")).json()["recorded_days"], 0)

    async def test_statistics_achievements_without_streaks(self):
        await self.register()
        for key in ("fajr", "dhuhr", "asr", "maghrib", "isha"):
            await self.client.patch(f"{self.day}/prayers/{key}", json={"state": 1 if key == "fajr" else 2})
        await self.client.patch("/api/v1/days/2026-09-20/prayers/fajr", json={"state": 1})
        await self.client.patch(f"{self.day}/sunnah", json={"key": "fajr_before", "value": 2})
        await self.client.patch(f"{self.day}/sunnah", json={"key": "witr", "value": True})
        stats = (await self.client.get("/api/v1/statistics")).json()
        self.assertEqual({key: stats[key] for key in ("recorded_days", "completed_days", "completed", "total", "home", "mosque", "sunnah_rakahs", "witr_days", "percent")},
                         {"recorded_days": 2, "completed_days": 1, "completed": 6, "total": 10, "home": 2, "mosque": 4, "sunnah_rakahs": 2, "witr_days": 1, "percent": 60})
        achievements = {item["key"]: item for item in stats["achievements"]}
        self.assertTrue(achievements["first_prayer"]["unlocked"])
        self.assertTrue(achievements["full_day"]["unlocked"])
        self.assertTrue(achievements["sunnah_start"]["unlocked"])
        self.assertFalse(achievements["five_full_days"]["unlocked"])
        self.assertEqual(achievements["five_full_days"]["progress"], 1)
        self.assertNotIn("streak", str(stats))

    async def test_cookie_csrf_mobile_and_security_headers(self):
        auth = await self.register()
        response = await self.client.patch(f"{self.day}/prayers/fajr", json={"state": 1}, headers={"Origin": "https://other.example"})
        self.assertEqual(response.status_code, 403)
        self.client.headers.pop("Origin")
        self.assertEqual((await self.client.post(f"{self.day}/prayers/fajr/cycle")).status_code, 403)
        mobile = self.new_client(origin=False)
        self.clients.append(mobile)
        response = await mobile.post(f"{self.day}/prayers/fajr/cycle", headers={"Authorization": f"Bearer {auth['access_token']}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertNotIn("access-control-allow-origin", response.headers)
        response = await mobile.post("/api/v1/auth/login", json={"alias": "نجم تجريبي", "password": self.password}, headers={"Origin": "https://other.example"})
        self.assertEqual(response.status_code, 403)

    async def test_auth_rate_limit_and_secure_cookie_configuration(self):
        limited = self.new_client(create_app(Path(self.temp.name) / "limited.sqlite3", auth_limit=2, cookie_secure=True))
        self.clients.append(limited)
        response = await limited.post("/api/v1/auth/register", json={"alias": "limit-star", "password": self.password})
        self.assertEqual(response.status_code, 201)
        self.assertIn("; Secure", response.headers["set-cookie"])
        payload = {"alias": "limit-star", "password": "wrong-password"}
        self.assertEqual((await limited.post("/api/v1/auth/login", json=payload)).status_code, 401)
        response = await limited.post("/api/v1/auth/login", json=payload)
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response.headers)


if __name__ == "__main__":
    unittest.main()
