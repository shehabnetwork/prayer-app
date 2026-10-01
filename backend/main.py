"""Same-origin FastAPI API. Run: python -m uvicorn backend.main:app."""

from __future__ import annotations

import base64
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import date as Date, timedelta
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator

from .schedule import CITIES, CITIES_BY_ID, ScheduleService


PRAYER_KEYS = ("fajr", "dhuhr", "asr", "maghrib", "isha")
SUNNAH_KEYS = ("fajr_before", "dhuhr_before", "dhuhr_after", "maghrib_after", "isha_after", "witr")
COOKIE_NAME = "prayer_session"
SESSION_SECONDS = 30 * 24 * 60 * 60
PASSWORD_ITERATIONS = 600_000
Version = Annotated[StrictInt, Field(ge=0)]
PrayerState = Annotated[StrictInt, Field(ge=0, le=2)]
PrayerKey = Literal["fajr", "dhuhr", "asr", "maghrib", "isha"]
SunnahKey = Literal["fajr_before", "dhuhr_before", "dhuhr_after", "maghrib_after", "isha_after", "witr"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def normalize_alias(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip()
    if not 3 <= len(value) <= 24 or not all(
        c.isalnum() or unicodedata.category(c) == "Mn" or c in " _-" for c in value
    ) or not any(c.isalnum() for c in value):
        raise ValueError("استخدم اسمًا مستعارًا من ٣ إلى ٢٤ حرفًا، دون بريد إلكتروني أو بيانات شخصية")
    return value


class Credentials(StrictModel):
    alias: str
    password: Annotated[str, Field(min_length=8, max_length=128)]

    @field_validator("alias")
    @classmethod
    def validate_alias(cls, value: str) -> str:
        return normalize_alias(value)

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("اختر كلمة مرور قوية وليست مسافات فقط")
        return value


class Prayers(StrictModel):
    fajr: PrayerState
    dhuhr: PrayerState
    asr: PrayerState
    maghrib: PrayerState
    isha: PrayerState


class Sunnah(StrictModel):
    fajr_before: StrictInt
    dhuhr_before: StrictInt
    dhuhr_after: StrictInt
    maghrib_after: StrictInt
    isha_after: StrictInt
    witr: StrictBool

    @field_validator("fajr_before", "dhuhr_before", "dhuhr_after", "maghrib_after", "isha_after")
    @classmethod
    def validate_rakahs(cls, value: int, info) -> int:
        allowed = (0, 2, 4) if info.field_name == "dhuhr_before" else (0, 2)
        if value not in allowed:
            raise ValueError("عدد الركعات غير صالح")
        return value


class DayPut(StrictModel):
    prayers: Prayers
    sunnah: Sunnah
    dhuhr_before_blocks: Annotated[list[StrictBool], Field(min_length=2, max_length=2)]
    version: Version

    @model_validator(mode="after")
    def validate_blocks(self):
        if self.sunnah.dhuhr_before != 2 * sum(self.dhuhr_before_blocks):
            raise ValueError("مجموع سنة الظهر يجب أن يطابق مجموعتي الركعتين")
        return self


class PrayerPatch(StrictModel):
    state: PrayerState
    version: Version | None = None


class CyclePatch(StrictModel):
    version: Version | None = None


class AccountPatch(StrictModel):
    city_id: str

    @field_validator("city_id")
    @classmethod
    def validate_city(cls, value: str) -> str:
        if value not in CITIES_BY_ID:
            raise ValueError("اختر مدينة من القائمة")
        return value


class SunnahPatch(StrictModel):
    key: SunnahKey
    value: StrictInt | StrictBool
    block: Annotated[StrictInt, Field(ge=0, le=1)] | None = None
    version: Version | None = None

    @model_validator(mode="after")
    def validate_choice(self):
        if self.key == "dhuhr_before":
            if self.block is None or type(self.value) is not bool:
                raise ValueError("اختر مجموعة سنة الظهر ٠ أو ١ وقيمة صح أو خطأ")
        elif self.key == "witr":
            if self.block is not None or type(self.value) is not bool:
                raise ValueError("قيمة الوتر يجب أن تكون صح أو خطأ")
        elif self.block is not None or type(self.value) is not int or self.value not in (0, 2):
            raise ValueError("قيمة السنة يجب أن تكون ٠ أو ٢")
        return self


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return "$".join((str(PASSWORD_ITERATIONS), base64.b64encode(salt).decode(), base64.b64encode(digest).decode()))


def password_matches(password: str, stored: str) -> bool:
    try:
        iterations, salt, expected = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(digest, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validated_date(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value, flags=re.ASCII):
        raise HTTPException(422, "اكتب التاريخ بالشكل سنة-شهر-يوم، مثل 2026-09-30")
    try:
        parsed = Date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, "هذا التاريخ غير صالح. اختر يومًا صحيحًا") from None
    if parsed.isoformat() != value:
        raise HTTPException(422, "صيغة التاريخ غير صحيحة")
    return value


def date_range(start: str | None, end: str | None, required: bool = False):
    if start is None and end is None and not required:
        return None, None
    if start is None or end is None:
        raise HTTPException(422, "اختر تاريخ البداية والنهاية معًا")
    start, end = validated_date(start), validated_date(end)
    distance = (Date.fromisoformat(end) - Date.fromisoformat(start)).days
    if distance < 0:
        raise HTTPException(422, "تاريخ النهاية يجب أن يكون بعد البداية أو في اليوم نفسه")
    if distance >= 366:
        raise HTTPException(422, "اختر فترة لا تزيد على ٣٦٦ يومًا")
    return start, end


def blank_day(day: str) -> dict:
    return {
        "date": day,
        "prayers": {key: 0 for key in PRAYER_KEYS},
        "sunnah": {key: (False if key == "witr" else 0) for key in SUNNAH_KEYS},
        "dhuhr_before_blocks": [False, False],
        "version": 0,
    }


def day_stats(day: dict, eligible_prayers=None, *, scheduled: bool = False) -> dict:
    if scheduled and eligible_prayers is None:
        return {
            "completed": None, "total": None, "home": None, "mosque": None,
            "sunnah_rakahs": None, "witr": None, "gold_medals": None,
            "silver_medals": None, "percent": None, "gold_target": None,
            "silver_target": None,
        }
    eligible = set(PRAYER_KEYS if eligible_prayers is None else eligible_prayers)
    values = [day["prayers"][key] for key in PRAYER_KEYS if key in eligible]
    completed = sum(value > 0 for value in values)
    silver_medals = 0
    if "fajr" in eligible:
        silver_medals += int(day["sunnah"]["fajr_before"] > 0)
    if "dhuhr" in eligible:
        silver_medals += sum(day["dhuhr_before_blocks"])
        silver_medals += int(day["sunnah"]["dhuhr_after"] > 0)
    if "maghrib" in eligible:
        silver_medals += int(day["sunnah"]["maghrib_after"] > 0)
    if "isha" in eligible:
        silver_medals += int(day["sunnah"]["isha_after"] > 0) + int(day["sunnah"]["witr"])
    result = {
        "completed": completed,
        "total": len(eligible),
        "home": values.count(1),
        "mosque": values.count(2),
        "sunnah_rakahs": (
            (day["sunnah"]["fajr_before"] if "fajr" in eligible else 0)
            + (day["sunnah"]["dhuhr_before"] + day["sunnah"]["dhuhr_after"] if "dhuhr" in eligible else 0)
            + (day["sunnah"]["maghrib_after"] if "maghrib" in eligible else 0)
            + (day["sunnah"]["isha_after"] if "isha" in eligible else 0)
        ),
        "witr": day["sunnah"]["witr"] if "isha" in eligible else False,
        "gold_medals": sum(1 if value == 1 else 27 if value == 2 else 0 for value in values),
        "silver_medals": silver_medals,
        "percent": round(completed / len(eligible) * 100) if eligible else 0,
    }
    if scheduled:
        result["gold_target"] = len(eligible) * 27
        result["silver_target"] = sum({"fajr": 1, "dhuhr": 3, "asr": 0, "maghrib": 1, "isha": 2}[key] for key in eligible)
    return result


def snapshot(day: dict, schedule: dict | None = None, *, scheduled: bool = False) -> dict:
    eligible = schedule.get("eligible_prayers") if schedule is not None else None
    result = {**day, "stats": day_stats(day, eligible, scheduled=scheduled)}
    if scheduled:
        result["schedule"] = schedule
    return result


def row_day(row: sqlite3.Row) -> dict:
    return {
        "date": row["date"],
        "prayers": json.loads(row["prayers"]),
        "sunnah": json.loads(row["sunnah"]),
        "dhuhr_before_blocks": json.loads(row["dhuhr_before_blocks"]),
        "version": row["version"],
    }


class Database:
    def __init__(self, path: str | Path):
        self.path = str(Path(path).expanduser().resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    alias TEXT NOT NULL,
                    alias_key TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS session_user ON sessions(user_id);
                CREATE TABLE IF NOT EXISTS days (
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    date TEXT NOT NULL,
                    prayers TEXT NOT NULL,
                    sunnah TEXT NOT NULL,
                    dhuhr_before_blocks TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(user_id, date)
                );
            """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
            if "city_id" not in columns:
                try:
                    conn.execute("ALTER TABLE users ADD COLUMN city_id TEXT")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
        # The file contains hashes and private progress: do not make it world-readable.
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_day(self, user_id: int, day: str) -> dict:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM days WHERE user_id=? AND date=?", (user_id, day)).fetchone()
            return row_day(row) if row else blank_day(day)

    def update_day(self, user_id: int, day: str, version: int | None, mutate) -> dict:
        with self.connect() as conn:
            # Serialize read-modify-write so rapid cycles and independent blocks do not overwrite each other.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM days WHERE user_id=? AND date=?", (user_id, day)).fetchone()
            record = row_day(row) if row else blank_day(day)
            if version is not None and version != record["version"]:
                raise HTTPException(409, "تم تحديث هذا اليوم في مكان آخر. حدّث الصفحة ثم حاول مجددًا")
            mutate(record)
            record["version"] += 1
            conn.execute("""
                INSERT INTO days(user_id,date,prayers,sunnah,dhuhr_before_blocks,version,updated_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(user_id,date) DO UPDATE SET
                    prayers=excluded.prayers,sunnah=excluded.sunnah,
                    dhuhr_before_blocks=excluded.dhuhr_before_blocks,
                    version=excluded.version,updated_at=excluded.updated_at
            """, (user_id, day, json.dumps(record["prayers"]), json.dumps(record["sunnah"]),
                  json.dumps(record["dhuhr_before_blocks"]), record["version"], time.time()))
            return snapshot(record)

    def get_days(self, user_id: int, start: str | None = None, end: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM days WHERE user_id=?", [user_id]
        if start is not None:
            sql += " AND date BETWEEN ? AND ?"
            params += [start, end]
        with self.connect() as conn:
            return [snapshot(row_day(row)) for row in conn.execute(sql + " ORDER BY date", params).fetchall()]


class AuthLimiter:
    def __init__(self, limit: int = 20, window: int = 600):
        self.limit, self.window = limit, window
        self.requests: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, request: Request):
        host = request.client.host if request.client else "unknown"
        now = time.monotonic()
        with self.lock:
            for key in list(self.requests):
                if not self.requests[key] or self.requests[key][-1] <= now - self.window:
                    del self.requests[key]
            attempts = self.requests[host]
            while attempts and attempts[0] <= now - self.window:
                attempts.popleft()
            if len(attempts) >= self.limit:
                raise HTTPException(429, "محاولات كثيرة. انتظر قليلًا ثم حاول مجددًا", headers={"Retry-After": str(self.window)})
            attempts.append(now)


def normalized_origin(value: str):
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            return None
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            return None
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None


def aggregate(days: list[dict]) -> dict:
    recorded = len(days)
    unknown = any(day["stats"]["total"] is None for day in days)
    known_days = [day for day in days if day["stats"]["total"] is not None]
    completed = sum(day["stats"]["completed"] for day in known_days)
    completed_days = sum(day["stats"]["total"] == 5 and day["stats"]["completed"] == 5 for day in days)
    sunnah_rakahs = sum(day["stats"]["sunnah_rakahs"] for day in known_days)
    witr_days = sum(day["stats"]["witr"] for day in known_days)
    definitions = [
        ("first_prayer", "بداية جميلة", "سجّلت أول صلاة، خطوة جميلة!", completed, 1),
        ("full_day", "يوم مكتمل", "سجّلت الصلوات الخمس في يوم واحد", completed_days, 1),
        ("five_full_days", "خطوات متواصلة", "خمسة أيام مكتملة، حتى لو لم تكن متتالية", completed_days, 5),
        ("sunnah_start", "خير إضافي", "سجّلت سنة أو وترًا", sunnah_rakahs + witr_days, 1),
    ]
    return {
        "recorded_days": recorded,
        "completed_days": completed_days,
        "completed": None if unknown else completed,
        "total": None if unknown else sum(day["stats"]["total"] for day in days),
        "home": None if unknown else sum(day["stats"]["home"] for day in known_days),
        "mosque": None if unknown else sum(day["stats"]["mosque"] for day in known_days),
        "sunnah_rakahs": None if unknown else sunnah_rakahs,
        "witr_days": None if unknown else witr_days,
        "gold_medals": None if unknown else sum(day["stats"]["gold_medals"] for day in known_days),
        "silver_medals": None if unknown else sum(day["stats"]["silver_medals"] for day in known_days),
        "gold_target": None if unknown else sum(day["stats"].get("gold_target", 135) for day in known_days),
        "silver_target": None if unknown else sum(day["stats"].get("silver_target", 7) for day in known_days),
        "percent": (None if unknown else
                    round(completed / sum(day["stats"]["total"] for day in days) * 100)
                    if sum(day["stats"]["total"] for day in days) else 0),
        "achievements": [
            {"key": key, "title": title, "description": description,
             "unlocked": progress >= target, "progress": min(progress, target), "target": target}
            for key, title, description, progress, target in definitions
        ],
    }


def create_app(db_path: str | Path | None = None, *, cookie_secure: bool | None = None,
               trusted_origin: str | None = None, auth_limit: int = 20,
               schedule_service: ScheduleService | None = None) -> FastAPI:
    db_path = db_path or os.environ.get("PRAYER_DB_PATH") or Path(__file__).parent / "data" / "prayer.sqlite3"
    cookie_secure = cookie_secure if cookie_secure is not None else os.environ.get("PRAYER_COOKIE_SECURE", "false").lower() == "true"
    trusted_origin = trusted_origin or os.environ.get("PRAYER_TRUSTED_ORIGIN")
    if trusted_origin and normalized_origin(trusted_origin) is None:
        raise ValueError("PRAYER_TRUSTED_ORIGIN must be an http(s) origin, without a path")
    database = Database(db_path)
    limiter = AuthLimiter(auth_limit)
    schedules = schedule_service or ScheduleService()
    # A dummy hash makes nonexistent aliases take the same password-verification path.
    dummy_password = password_hash(secrets.token_urlsafe(32))
    app = FastAPI(title="صلاتي — Prayer Tracker", version="1.0.0", docs_url="/api/docs", redoc_url=None)
    app.state.database = database
    app.state.auth_limiter = limiter
    app.state.schedule_service = schedules

    @app.middleware("http")
    async def safe_headers_and_origin(request: Request, call_next):
        is_write = request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/v1/")
        origin = request.headers.get("origin")
        bearer = request.headers.get("authorization", "").lower().startswith("bearer ")
        cookie_write = COOKIE_NAME in request.cookies and not bearer
        if is_write and (origin is not None or cookie_write):
            expected = normalized_origin(trusted_origin or str(request.base_url))
            if origin is None or normalized_origin(origin) != expected:
                response = JSONResponse({"detail": "لأمان حسابك، أرسل التغيير من صفحة التطبيق نفسها"}, status_code=403)
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if not request.url.path.startswith("/api/docs"):
            response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def friendly_validation(request: Request, exc: RequestValidationError):
        error = exc.errors()[0] if exc.errors() else {}
        location = error.get("loc", ())
        if "password" in location:
            detail = "اختر كلمة مرور من ٨ إلى ١٢٨ حرفًا، وليست مسافات فقط"
        elif "alias" in location:
            detail = "استخدم اسمًا مستعارًا من ٣ إلى ٢٤ حرفًا، دون بريد إلكتروني أو بيانات شخصية"
        elif "prayer_key" in location:
            detail = "اختر صلاة من الفجر والظهر والعصر والمغرب والعشاء"
        elif "state" in location:
            detail = "حالة الصلاة يجب أن تكون ٠ أو ١ (في البيت) أو ٢ (في المسجد)"
        elif "version" in location:
            detail = "أرسل رقم نسخة اليوم الصحيح مع التحديث"
        else:
            detail = "البيانات غير صالحة. راجع القيم المطلوبة وحاول مجددًا"
        # Never echo submitted passwords or other raw validation inputs.
        return JSONResponse({"detail": detail}, status_code=422)

    def session_token(request: Request) -> str | None:
        authorization = request.headers.get("authorization")
        if authorization is not None:
            scheme, _, value = authorization.partition(" ")
            if scheme.lower() != "bearer" or not value or len(value) > 512:
                raise HTTPException(401, "سجّل الدخول أولًا", headers={"WWW-Authenticate": "Bearer"})
            return value
        return request.cookies.get(COOKIE_NAME)

    def current_user(request: Request) -> dict:
        token = session_token(request)
        if not token or len(token) > 512:
            raise HTTPException(401, "سجّل الدخول أولًا", headers={"WWW-Authenticate": "Bearer"})
        with database.connect() as conn:
            row = conn.execute("""
                SELECT users.id,users.alias,users.city_id FROM sessions JOIN users ON users.id=sessions.user_id
                WHERE sessions.token_hash=? AND sessions.expires_at>?
            """, (token_hash(token), time.time())).fetchone()
        if row is None:
            raise HTTPException(401, "انتهت الجلسة. سجّل الدخول مجددًا", headers={"WWW-Authenticate": "Bearer"})
        return user_view(row)

    def user_view(user) -> dict:
        city_id = user["city_id"] if "city_id" in user.keys() else user.get("city_id")
        city = CITIES_BY_ID.get(city_id)
        return {
            "id": user["id"], "alias": user["alias"], "city_id": city_id,
            "city_name": city["name"] if city else None,
            "timezone": city["timezone"] if city else None,
            "today": schedules.today(city) if city else None,
        }

    def schedule_for(user: dict, day: str) -> dict | None:
        city = CITIES_BY_ID.get(user.get("city_id"))
        return schedules.schedule(city, day) if city else None

    def eligible_for_mutation(user: dict, day: str) -> set[str] | None:
        city = CITIES_BY_ID.get(user.get("city_id"))
        if city is None:
            return None
        today = schedules.today(city)
        if day < today:
            return set(PRAYER_KEYS)
        if day > today:
            return set()
        schedule = schedules.schedule(city, day)
        if not schedule["available"]:
            raise HTTPException(503, schedule["error"])
        return set(schedule["eligible_prayers"])

    def require_eligible(user: dict, day: str, prayer_key: str):
        eligible = eligible_for_mutation(user, day)
        if eligible is not None and prayer_key not in eligible:
            raise HTTPException(409, "لم يحن وقت هذه الصلاة بعد")

    def selected_snapshot(record: dict, user: dict) -> dict:
        city = CITIES_BY_ID.get(user.get("city_id"))
        if city is None:
            return snapshot(record)
        schedule = schedule_for(user, record["date"])
        eligible = schedule["eligible_prayers"]
        if not schedule["available"]:
            if record["date"] < schedule["today"]:
                eligible = list(PRAYER_KEYS)
            elif record["date"] > schedule["today"]:
                eligible = []
        return {**record, "stats": day_stats(record, eligible, scheduled=True), "schedule": schedule}

    def statistical_snapshot(record: dict, user: dict) -> dict:
        city = CITIES_BY_ID.get(user.get("city_id"))
        if city is None:
            return snapshot(record)
        today = schedules.today(city)
        if record["date"] < today:
            eligible = list(PRAYER_KEYS)
        elif record["date"] > today:
            eligible = []
        else:
            schedule = schedules.schedule(city, record["date"])
            eligible = schedule["eligible_prayers"] if schedule["available"] else None
        return snapshot(record, {"eligible_prayers": eligible}, scheduled=True)

    def history_snapshot(record: dict, user: dict) -> dict:
        city = CITIES_BY_ID[user["city_id"]]
        today = schedules.today(city)
        if record["date"] < today:
            eligible = list(PRAYER_KEYS)
            schedule = {"available": False, "timezone": city["timezone"], "today": today,
                        "times": None, "eligible_prayers": eligible,
                        "error": "المواقيت متاحة عند فتح اليوم"}
        elif record["date"] > today:
            eligible = []
            schedule = {"available": False, "timezone": city["timezone"], "today": today,
                        "times": None, "eligible_prayers": eligible,
                        "error": "المواقيت متاحة عند فتح اليوم"}
        else:
            schedule = schedules.schedule(city, record["date"])
        return snapshot(record, schedule, scheduled=True)

    def issue_session(conn, user: dict, response: Response) -> dict:
        token = secrets.token_urlsafe(32)
        conn.execute("DELETE FROM sessions WHERE expires_at<=?", (time.time(),))
        conn.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)",
                     (token_hash(token), user["id"], time.time() + SESSION_SECONDS))
        response.set_cookie(COOKIE_NAME, token, max_age=SESSION_SECONDS, httponly=True,
                            secure=cookie_secure, samesite="lax", path="/")
        return {"user": user, "access_token": token, "token_type": "bearer"}

    @app.get("/api/v1/health")
    def health():
        return {"ok": True}

    @app.post("/api/v1/auth/register", status_code=201)
    def register(body: Credentials, response: Response, request: Request):
        limiter.check(request)
        stored_hash = password_hash(body.password)
        try:
            with database.connect() as conn:
                cursor = conn.execute("INSERT INTO users(alias,alias_key,password_hash,created_at) VALUES(?,?,?,?)",
                                      (body.alias, body.alias.casefold(), stored_hash, time.time()))
                return issue_session(conn, user_view({"id": cursor.lastrowid, "alias": body.alias, "city_id": None}), response)
        except sqlite3.IntegrityError:
            raise HTTPException(409, "هذا الاسم المستعار مستخدم. اختر اسمًا آخر") from None

    @app.post("/api/v1/auth/login")
    def login(body: Credentials, response: Response, request: Request):
        limiter.check(request)
        with database.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE alias_key=?", (body.alias.casefold(),)).fetchone()
            valid = password_matches(body.password, row["password_hash"] if row else dummy_password)
            if row is None or not valid:
                raise HTTPException(401, "الاسم المستعار أو كلمة المرور غير صحيحة")
            return issue_session(conn, user_view(row), response)

    @app.get("/api/v1/auth/me")
    def me(user: dict = Depends(current_user)):
        return {"user": user}

    @app.get("/api/v1/cities")
    def cities():
        return {"cities": [
            {key: city[key] for key in ("id", "name", "name_en", "country", "timezone")}
            for city in CITIES
        ]}

    @app.patch("/api/v1/account")
    def patch_account(body: AccountPatch, user: dict = Depends(current_user)):
        with database.connect() as conn:
            conn.execute("UPDATE users SET city_id=? WHERE id=?", (body.city_id, user["id"]))
        return {"user": user_view({**user, "city_id": body.city_id})}

    @app.post("/api/v1/auth/logout")
    def logout(request: Request, response: Response, user: dict = Depends(current_user)):
        with database.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(session_token(request)),))
        response.delete_cookie(COOKIE_NAME, path="/", secure=cookie_secure, httponly=True, samesite="lax")
        return {"ok": True}

    @app.get("/api/v1/days/{day}")
    def get_day(day: str, user: dict = Depends(current_user)):
        day = validated_date(day)
        return selected_snapshot(database.get_day(user["id"], day), user)

    @app.put("/api/v1/days/{day}")
    def put_day(day: str, body: DayPut, user: dict = Depends(current_user)):
        day = validated_date(day)
        eligible = eligible_for_mutation(user, day)
        current = database.get_day(user["id"], day)
        if eligible is not None:
            changed = {key for key in PRAYER_KEYS if current["prayers"][key] != getattr(body.prayers, key)}
            sunnah_prayer = {"fajr_before": "fajr", "dhuhr_before": "dhuhr", "dhuhr_after": "dhuhr",
                              "maghrib_after": "maghrib", "isha_after": "isha", "witr": "isha"}
            changed.update(sunnah_prayer[key] for key in SUNNAH_KEYS
                           if current["sunnah"][key] != getattr(body.sunnah, key))
            if current["dhuhr_before_blocks"] != body.dhuhr_before_blocks:
                changed.add("dhuhr")
            if not changed.issubset(eligible):
                raise HTTPException(409, "لا يمكن تحديث صلاة لم يحن وقتها بعد")
        def mutate(record):
            record["prayers"] = body.prayers.model_dump()
            record["sunnah"] = body.sunnah.model_dump()
            record["dhuhr_before_blocks"] = body.dhuhr_before_blocks.copy()
        database.update_day(user["id"], day, body.version, mutate)
        return selected_snapshot(database.get_day(user["id"], day), user)

    @app.patch("/api/v1/days/{day}/prayers/{prayer_key}")
    def patch_prayer(day: str, prayer_key: PrayerKey, body: PrayerPatch, user: dict = Depends(current_user)):
        day = validated_date(day)
        require_eligible(user, day, prayer_key)
        def mutate(record):
            record["prayers"][prayer_key] = body.state
        database.update_day(user["id"], day, body.version, mutate)
        return selected_snapshot(database.get_day(user["id"], day), user)

    @app.post("/api/v1/days/{day}/prayers/{prayer_key}/cycle")
    def cycle_prayer(day: str, prayer_key: PrayerKey, body: CyclePatch | None = Body(default=None), user: dict = Depends(current_user)):
        day = validated_date(day)
        require_eligible(user, day, prayer_key)
        def mutate(record):
            record["prayers"][prayer_key] = (record["prayers"][prayer_key] + 1) % 3
        database.update_day(user["id"], day, body.version if body else None, mutate)
        return selected_snapshot(database.get_day(user["id"], day), user)

    @app.patch("/api/v1/days/{day}/sunnah")
    def patch_sunnah(day: str, body: SunnahPatch, user: dict = Depends(current_user)):
        day = validated_date(day)
        prayer_key = {"fajr_before": "fajr", "dhuhr_before": "dhuhr", "dhuhr_after": "dhuhr",
                      "maghrib_after": "maghrib", "isha_after": "isha", "witr": "isha"}[body.key]
        require_eligible(user, day, prayer_key)
        def mutate(record):
            if body.key == "dhuhr_before":
                record["dhuhr_before_blocks"][body.block] = body.value
                record["sunnah"]["dhuhr_before"] = 2 * sum(record["dhuhr_before_blocks"])
            else:
                record["sunnah"][body.key] = body.value
        database.update_day(user["id"], day, body.version, mutate)
        return selected_snapshot(database.get_day(user["id"], day), user)

    @app.get("/api/v1/history")
    def history(start: str | None = Query(default=None, alias="from"), end: str | None = Query(default=None, alias="to"), user: dict = Depends(current_user)):
        start, end = date_range(start, end, required=True)
        days = database.get_days(user["id"], start, end)
        if user.get("city_id"):
            days = [history_snapshot({key: value for key, value in day.items() if key != "stats"}, user) for day in days]
        return {"from": start, "to": end, "days": days}

    @app.get("/api/v1/statistics")
    def statistics(start: str | None = Query(default=None, alias="from"), end: str | None = Query(default=None, alias="to"), user: dict = Depends(current_user)):
        start, end = date_range(start, end)
        days = database.get_days(user["id"], start, end)
        if user.get("city_id"):
            days = [statistical_snapshot({key: value for key, value in day.items() if key != "stats"}, user) for day in days]
        return aggregate(days)

    frontend = Path(__file__).resolve().parent.parent / "frontend"
    if frontend.exists():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    return app


app = create_app()
