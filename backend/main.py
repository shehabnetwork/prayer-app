"""Same-origin FastAPI API. Run: python -m uvicorn backend.main:app."""

from __future__ import annotations

import base64
from collections import defaultdict, deque
from datetime import date as Date, datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
import unicodedata
from typing import Annotated, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator

from .database import Database, is_unique_violation
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
        raise ValueError("استخدم اسم مستخدم من ٣ إلى ٢٤ حرفًا، دون بريد إلكتروني أو بيانات شخصية")
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


def normalize_display_name(value: str) -> str:
    if any(unicodedata.category(c).startswith("C") for c in value):
        raise ValueError("اختر اسم عرض من ١ إلى ٨٠ حرفًا دون محارف تحكم")
    value = value.strip()
    if not 1 <= len(value) <= 80:
        raise ValueError("اختر اسم عرض من ١ إلى ٨٠ حرفًا دون محارف تحكم")
    return value


class Registration(Credentials):
    display_name: str = ""

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        return normalize_display_name(value)

    @model_validator(mode="after")
    def default_display_name(self):
        if "display_name" not in self.model_fields_set:
            self.display_name = self.alias
        return self

    account_type: Literal["child", "supervisor"] = "child"


class InviteToken(StrictModel):
    token: Annotated[str, Field(min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]


class InviteAcceptance(InviteToken):
    request_key: Annotated[str, Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]


class GroupCreate(StrictModel):
    name: Annotated[str, Field(min_length=1, max_length=80)]
    members_can_view_records: StrictBool = False

    @field_validator("name")
    @classmethod
    def validate_name(cls, value):
        if value is None:
            raise ValueError("اسم المجموعة غير صالح")
        value = unicodedata.normalize("NFKC", value).strip()
        if not value or any(unicodedata.category(c).startswith("C") for c in value):
            raise ValueError("اسم المجموعة غير صالح")
        return value


class GroupPatch(StrictModel):
    name: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    members_can_view_records: StrictBool | None = None
    validate_name = field_validator("name")(GroupCreate.validate_name.__func__)

    @model_validator(mode="after")
    def validate_update(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("اختر تحديثًا صالحًا")
        return self


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
    display_name: str | None = None

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("اختر اسم عرض صالحًا")
        return normalize_display_name(value)

    city_id: str | None = None
    can_supervise: StrictBool | None = None

    @field_validator("city_id")
    @classmethod
    def validate_city(cls, value: str | None) -> str:
        if value not in CITIES_BY_ID:
            raise ValueError("اختر مدينة من القائمة")
        return value

    @model_validator(mode="after")
    def validate_update(self):
        if not self.model_fields_set or ("can_supervise" in self.model_fields_set and self.can_supervise is not True):
            raise ValueError("يمكن تفعيل الإشراف فقط")
        return self


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


def row_day(row) -> dict:
    return {
        "date": row["date"],
        "prayers": json.loads(row["prayers"]),
        "sunnah": json.loads(row["sunnah"]),
        "dhuhr_before_blocks": json.loads(row["dhuhr_before_blocks"]),
        "version": row["version"],
    }




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
    configured_url = os.environ.get("PRAYER_DATABASE_URL")
    if db_path is None and configured_url is not None and not configured_url.startswith(("postgres://", "postgresql://")):
        raise ValueError("PRAYER_DATABASE_URL must be a PostgreSQL URL")
    db_path = db_path if db_path is not None else (configured_url or os.environ.get("PRAYER_DB_PATH") or Path(__file__).parent / "data" / "prayer.sqlite3")
    cookie_secure = cookie_secure if cookie_secure is not None else os.environ.get("PRAYER_COOKIE_SECURE", "false").lower() == "true"
    trusted_origin = trusted_origin or os.environ.get("PRAYER_TRUSTED_ORIGIN")
    if trusted_origin and normalized_origin(trusted_origin) is None:
        raise ValueError("PRAYER_TRUSTED_ORIGIN must be an http(s) origin, without a path")
    database = Database(db_path)
    limiter = AuthLimiter(auth_limit)
    invite_write_limiter = AuthLimiter(limit=60, window=600)
    invite_lookup_limiter = AuthLimiter(limit=600, window=600)
    schedules = schedule_service or ScheduleService()
    # A dummy hash makes nonexistent aliases take the same password-verification path.
    dummy_password = password_hash(secrets.token_urlsafe(32))
    app = FastAPI(title="صلاتي — Prayer Tracker", version="1.0.0", docs_url="/api/docs", redoc_url=None)
    app.state.database = database
    app.state.auth_limiter = limiter
    app.state.schedule_service = schedules

    @app.middleware("http")
    async def safe_headers_and_origin(request: Request, call_next):
        path = request.url.path
        retired = path.startswith(("/api/v1/supervisor/", "/api/v1/supervisor-invites/", "/api/v1/account/supervisors"))
        is_write = request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/v1/")
        origin = request.headers.get("origin")
        bearer = request.headers.get("authorization", "").lower().startswith("bearer ")
        cookie_write = COOKIE_NAME in request.cookies and not bearer
        if retired:
            response = JSONResponse({"detail": "الإشراف القديم متوقف. استخدم المجموعات"}, status_code=410)
        elif is_write and (origin is not None or cookie_write):
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
        elif "display_name" in location:
            detail = "اختر اسم عرض من ١ إلى ٨٠ حرفًا دون محارف تحكم"
        elif "alias" in location:
            detail = "استخدم اسم مستخدم من ٣ إلى ٢٤ حرفًا، دون بريد إلكتروني أو بيانات شخصية"
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
                SELECT users.id,users.alias,users.display_name,users.city_id,users.can_supervise FROM sessions JOIN users ON users.id=sessions.user_id
                WHERE sessions.token_hash=? AND sessions.expires_at>?
            """, (token_hash(token), time.time())).fetchone()
        if row is None:
            raise HTTPException(401, "انتهت الجلسة. سجّل الدخول مجددًا", headers={"WWW-Authenticate": "Bearer"})
        return user_view(row)

    def user_view(user) -> dict:
        city_id = user["city_id"] if "city_id" in user.keys() else user.get("city_id")
        city = CITIES_BY_ID.get(city_id)
        return {
            "id": user["id"], "alias": user["alias"], "display_name": user["display_name"], "city_id": city_id,
            "can_supervise": bool(user["can_supervise"]),
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
    def register(body: Registration, response: Response, request: Request):
        limiter.check(request)
        stored_hash = password_hash(body.password)
        try:
            with database.connect() as conn:
                can_supervise = body.account_type == "supervisor"
                user_id = conn.insert_id("INSERT INTO users(alias,alias_key,password_hash,created_at,can_supervise,display_name) VALUES(?,?,?,?,?,?)",
                                      (body.alias, body.alias.casefold(), stored_hash, time.time(), int(can_supervise), body.display_name))
                return issue_session(conn, user_view({"id": user_id, "alias": body.alias, "display_name": body.display_name, "city_id": None,
                                                      "can_supervise": can_supervise}), response)
        except Exception as exc:
            if not is_unique_violation(exc):
                raise
            raise HTTPException(409, "اسم المستخدم هذا مستخدم. اختر اسمًا آخر") from None

    @app.post("/api/v1/auth/login")
    def login(body: Credentials, response: Response, request: Request):
        limiter.check(request)
        with database.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE alias_key=?", (body.alias.casefold(),)).fetchone()
            valid = password_matches(body.password, row["password_hash"] if row else dummy_password)
            if row is None or not valid:
                raise HTTPException(401, "اسم المستخدم أو كلمة المرور غير صحيحة")
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
            updates = body.model_dump(exclude_unset=True)
            if "can_supervise" in updates:
                updates["can_supervise"] = int(updates["can_supervise"])
            assignments = ",".join(f"{key}=?" for key in updates)
            conn.execute(f"UPDATE users SET {assignments} WHERE id=?", (*updates.values(), user["id"]))
            row = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
        return {"user": user_view(row)}

    def group_view(conn, group, user_id):
        return {**dict(group), 'members_can_view_records': bool(group['members_can_view_records']),
                'is_admin': group['admin_id'] == user_id,
                'member_count': conn.execute('SELECT COUNT(*) FROM group_members WHERE group_id=?', (group['id'],)).fetchone()[0]}

    def require_group(conn, group_id, user, *, admin=False, records=False):
        group = conn.execute('SELECT groups.* FROM groups JOIN group_members ON group_members.group_id=groups.id WHERE groups.id=? AND group_members.user_id=?', (group_id, user['id'])).fetchone()
        if group is None or (admin and group['admin_id'] != user['id']) or (records and group['admin_id'] != user['id'] and not group['members_can_view_records']):
            raise HTTPException(403, 'لا تملك صلاحية الوصول إلى المجموعة')
        return group

    @app.get('/api/v1/groups')
    def list_groups(user: dict = Depends(current_user)):
        with database.connect() as conn:
            rows = conn.execute('SELECT groups.* FROM groups JOIN group_members ON group_id=groups.id WHERE user_id=? ORDER BY groups.id', (user['id'],)).fetchall()
            return {'groups': [group_view(conn, row, user['id']) for row in rows]}

    @app.post('/api/v1/groups', status_code=201)
    def post_group(body: GroupCreate, user: dict = Depends(current_user)):
        with database.connect() as conn:
            conn.lock_users([user['id']])
            now = time.time()
            gid = conn.insert_id('INSERT INTO groups(name,admin_id,members_can_view_records,created_at) VALUES(?,?,?,?)', (body.name, user['id'], int(body.members_can_view_records), now))
            conn.execute('INSERT INTO group_members(group_id,user_id,created_at) VALUES(?,?,?)', (gid, user['id'], now))
            canonical_group_invite(conn, gid)
            return {'group': group_view(conn, require_group(conn, gid, user), user['id'])}

    @app.get('/api/v1/groups/{group_id}')
    def get_group(group_id: int, user: dict = Depends(current_user)):
        with database.connect() as conn:
            group = require_group(conn, group_id, user)
            rows = conn.execute('SELECT users.* FROM users JOIN group_members ON user_id=users.id WHERE group_id=? ORDER BY users.id', (group_id,)).fetchall()
            result = {'group': group_view(conn, group, user['id']), 'can_view_records': group['admin_id'] == user['id'] or bool(group['members_can_view_records'])}
        result['members'] = [{**user_view(row), 'is_admin': row['id'] == group['admin_id']} for row in rows]
        with database.connect() as conn:
            latest = require_group(conn, group_id, user)
            authorized = {row[0] for row in conn.execute('SELECT user_id FROM group_members WHERE group_id=?', (group_id,))}
            result['group'] = group_view(conn, latest, user['id'])
            result['can_view_records'] = latest['admin_id'] == user['id'] or bool(latest['members_can_view_records'])
        result['members'] = [row for row in result['members'] if row['id'] in authorized]
        return result

    @app.patch('/api/v1/groups/{group_id}')
    def patch_group(group_id: int, body: GroupPatch, user: dict = Depends(current_user)):
        with database.connect() as conn:
            conn.lock_group(group_id)
            require_group(conn, group_id, user, admin=True)
            updates = body.model_dump(exclude_unset=True)
            if 'members_can_view_records' in updates:
                updates['members_can_view_records'] = int(updates['members_can_view_records'])
            conn.execute('UPDATE groups SET ' + ','.join(f'{key}=?' for key in updates) + ' WHERE id=?', (*updates.values(), group_id))
            return {'group': group_view(conn, require_group(conn, group_id, user), user['id'])}

    def delete_membership(group_id, member_id, user, admin):
        with database.connect() as conn:
            conn.lock_group(group_id)
            group = require_group(conn, group_id, user, admin=admin)
            if group['admin_id'] == member_id:
                raise HTTPException(409, 'لا يمكن إزالة مدير المجموعة أو مغادرته')
            conn.execute('DELETE FROM group_members WHERE group_id=? AND user_id=?', (group_id, member_id))
        return {'ok': True}

    @app.delete('/api/v1/groups/{group_id}')
    def delete_group(group_id: int, user: dict = Depends(current_user)):
        with database.connect() as conn:
            conn.lock_group(group_id)
            require_group(conn, group_id, user, admin=True)
            # Receipts reference invitations without a cascading foreign key.
            # Delete only group-owned data; personal users and days remain intact.
            conn.execute('DELETE FROM group_invite_acceptances WHERE invite_id IN (SELECT id FROM group_invites WHERE group_id=?)', (group_id,))
            conn.execute('DELETE FROM group_invites WHERE group_id=?', (group_id,))
            conn.execute('DELETE FROM group_members WHERE group_id=?', (group_id,))
            conn.execute('DELETE FROM groups WHERE id=?', (group_id,))
        return {'ok': True}

    @app.delete('/api/v1/groups/{group_id}/membership')
    def leave_group(group_id: int, user: dict = Depends(current_user)):
        return delete_membership(group_id, user['id'], user, False)

    @app.delete('/api/v1/groups/{group_id}/members/{member_id}')
    def remove_member(group_id: int, member_id: int, user: dict = Depends(current_user)):
        return delete_membership(group_id, member_id, user, True)

    def invite_view(row):
        return {key: row[key] for key in ('id', 'created_at', 'revoked_at')}

    def canonical_group_invite(conn, group_id):
        row = conn.execute('SELECT * FROM group_invites WHERE group_id=? AND revoked_at IS NULL AND share_token IS NOT NULL', (group_id,)).fetchone()
        if row is None:
            token, now = secrets.token_urlsafe(32), time.time()
            iid = conn.insert_id('INSERT INTO group_invites(group_id,token_hash,created_at,share_token) VALUES(?,?,?,?)', (group_id, token_hash(token), now, token))
            row = {'id': iid, 'created_at': now, 'revoked_at': None, 'share_token': token}
        token = row['share_token']
        return {'invite': invite_view(row), 'token': token,
                'link': f"{trusted_origin.rstrip('/')}/#invite={token}" if trusted_origin else None}

    @app.post('/api/v1/groups/{group_id}/invite-link')
    def get_group_invite_link(group_id: int, user: dict = Depends(current_user)):
        with database.connect() as conn:
            conn.lock_group(group_id)
            require_group(conn, group_id, user, admin=True)
            return canonical_group_invite(conn, group_id)

    def create_group_invite(group_id, user, rotate):
        token, now = secrets.token_urlsafe(32), time.time()
        with database.connect() as conn:
            conn.lock_group(group_id)
            require_group(conn, group_id, user, admin=True)
            if rotate:
                conn.execute('UPDATE group_invites SET revoked_at=? WHERE group_id=? AND revoked_at IS NULL', (now, group_id))
            elif conn.execute('SELECT 1 FROM group_invites WHERE group_id=? AND revoked_at IS NULL', (group_id,)).fetchone():
                raise HTTPException(409, 'لديك رابط نشط بالفعل. استبدله إذا فقدت نسخته')
            iid = conn.insert_id('INSERT INTO group_invites(group_id,token_hash,created_at,share_token) VALUES(?,?,?,?)', (group_id, token_hash(token), now, token))
        return {'invite': {'id': iid, 'created_at': now, 'revoked_at': None}, 'token': token,
                'link': f"{trusted_origin.rstrip('/')}/#invite={token}" if trusted_origin else None}

    @app.post('/api/v1/groups/{group_id}/invites', status_code=201)
    def post_group_invite(group_id: int, request: Request, user: dict = Depends(current_user)):
        invite_write_limiter.check(request)
        return create_group_invite(group_id, user, False)

    @app.post('/api/v1/groups/{group_id}/invites/rotate', status_code=201)
    def rotate_group_invite(group_id: int, request: Request, user: dict = Depends(current_user)):
        invite_write_limiter.check(request)
        return create_group_invite(group_id, user, True)

    @app.get('/api/v1/groups/{group_id}/invites')
    def list_group_invites(group_id: int, user: dict = Depends(current_user)):
        with database.connect() as conn:
            require_group(conn, group_id, user, admin=True)
            return {'invites': [invite_view(row) for row in conn.execute('SELECT * FROM group_invites WHERE group_id=? ORDER BY id DESC', (group_id,))]}

    @app.delete('/api/v1/groups/{group_id}/invites/{invite_id}')
    def revoke_group_invite(group_id: int, invite_id: int, user: dict = Depends(current_user)):
        with database.connect() as conn:
            conn.lock_group(group_id)
            require_group(conn, group_id, user, admin=True)
            if not conn.execute('UPDATE group_invites SET revoked_at=COALESCE(revoked_at,?) WHERE id=? AND group_id=?', (time.time(), invite_id, group_id)).rowcount:
                raise HTTPException(404, 'الرابط غير موجود')
        return {'ok': True}

    def invite_group_view(conn, gid):
        row = conn.execute('SELECT groups.*,users.alias AS admin_alias,users.display_name AS admin_display_name FROM groups JOIN users ON users.id=admin_id WHERE groups.id=?', (gid,)).fetchone()
        if row is None:
            raise HTTPException(404, 'رابط المجموعة غير صالح أو تم إلغاؤه')
        return {key: (bool(row[key]) if key == 'members_can_view_records' else row[key]) for key in ('id', 'name', 'admin_id', 'members_can_view_records', 'admin_alias', 'admin_display_name')}

    @app.post('/api/v1/group-invites/preview')
    def preview_group_invite(body: InviteToken, request: Request):
        invite_lookup_limiter.check(request)
        with database.connect() as conn:
            row = conn.execute('SELECT group_id FROM group_invites WHERE token_hash=? AND revoked_at IS NULL', (token_hash(body.token),)).fetchone()
            if row is None:
                raise HTTPException(404, 'رابط المجموعة غير صالح أو تم إلغاؤه')
            return {'group': invite_group_view(conn, row['group_id']), 'scope': 'prayer_history_read'}

    @app.post('/api/v1/group-invites/accept')
    def accept_group_invite(body: InviteAcceptance, request: Request, user: dict = Depends(current_user)):
        invite_lookup_limiter.check(request)
        with database.connect() as conn:
            row = conn.execute('SELECT * FROM group_invites WHERE token_hash=?', (token_hash(body.token),)).fetchone()
            conn.lock_group(row['group_id'] if row else 0)
            # User lock also serializes request-key uniqueness across different groups.
            if database.dialect == 'postgres':
                conn.lock_users([user['id']])
            previous = conn.execute('SELECT group_invites.* FROM group_invite_acceptances JOIN group_invites ON invite_id=group_invites.id WHERE user_id=? AND request_key=?', (user['id'], body.request_key)).fetchone()
            if previous:
                if not hmac.compare_digest(previous['token_hash'], token_hash(body.token)):
                    raise HTTPException(409, 'استُخدم مفتاح الطلب لرابط آخر')
                if not conn.execute('SELECT 1 FROM group_members WHERE group_id=? AND user_id=?', (previous['group_id'], user['id'])).fetchone():
                    raise HTTPException(409, 'أُزيلت العضوية. أكد الانضمام من جديد بطلب جديد')
                return {'ok': True, 'group': invite_group_view(conn, previous['group_id'])}
            row = conn.execute('SELECT * FROM group_invites WHERE token_hash=? AND revoked_at IS NULL', (token_hash(body.token),)).fetchone()
            if row is None:
                raise HTTPException(404, 'رابط المجموعة غير صالح أو تم إلغاؤه')
            now = time.time()
            conn.execute('INSERT INTO group_members(group_id,user_id,created_at) VALUES(?,?,?) ON CONFLICT(group_id,user_id) DO NOTHING', (row['group_id'], user['id'], now))
            conn.execute('INSERT INTO group_invite_acceptances(invite_id,user_id,request_key,accepted_at) VALUES(?,?,?,?)', (row['id'], user['id'], body.request_key, now))
            return {'ok': True, 'group': invite_group_view(conn, row['group_id'])}

    def group_member(group_id, member_id, user, *, view=True):
        with database.connect() as conn:
            # Resolve both memberships and visibility in the same SQL snapshot.
            # In PostgreSQL READ COMMITTED, separate checks could observe a
            # removed viewer with a still-current target membership.
            row = conn.execute('''
                SELECT users.* FROM users
                JOIN group_members target ON target.user_id=users.id
                JOIN groups ON groups.id=target.group_id
                JOIN group_members viewer ON viewer.group_id=groups.id
                WHERE groups.id=? AND target.user_id=? AND viewer.user_id=?
                  AND (viewer.user_id=target.user_id OR groups.admin_id=viewer.user_id
                       OR groups.members_can_view_records=1)
            ''', (group_id, member_id, user['id'])).fetchone()
        if row is None:
            raise HTTPException(403, 'لا تملك صلاحية قراءة سجل هذا العضو')
        return user_view(row) if view else None

    @app.get('/api/v1/groups/{group_id}/members/{member_id}/days/{day}')
    def member_day(group_id: int, member_id: int, day: str, user: dict = Depends(current_user)):
        result = get_day(day, group_member(group_id, member_id, user))
        group_member(group_id, member_id, user, view=False)
        return result

    @app.get('/api/v1/groups/{group_id}/members/{member_id}/history')
    def member_history(group_id: int, member_id: int, start: str | None = Query(default=None, alias='from'), end: str | None = Query(default=None, alias='to'), user: dict = Depends(current_user)):
        result = history(start, end, group_member(group_id, member_id, user))
        group_member(group_id, member_id, user, view=False)
        return result

    @app.get('/api/v1/groups/{group_id}/members/{member_id}/statistics')
    def member_statistics(group_id: int, member_id: int, start: str | None = Query(default=None, alias='from'), end: str | None = Query(default=None, alias='to'), user: dict = Depends(current_user)):
        result = statistics(start, end, group_member(group_id, member_id, user))
        group_member(group_id, member_id, user, view=False)
        return result

    @app.get("/api/v1/groups/{group_id}/daily-review")
    def daily_review(group_id: int, date: str, limit: int = Query(default=50, ge=1, le=100),
                     offset: int = Query(default=0, ge=0), user: dict = Depends(current_user)):
        date = validated_date(date)
        instant = datetime.now(timezone.utc)
        with database.connect() as conn:
            require_group(conn, group_id, user, records=True)
            roster = conn.execute("""
                SELECT users.id, users.alias, users.display_name, users.alias_key, users.city_id,
                       days.date, days.prayers, days.sunnah, days.dhuhr_before_blocks, days.version
                FROM group_members JOIN users ON users.id=group_members.user_id
                LEFT JOIN days ON days.user_id=users.id AND days.date=?
                WHERE group_members.group_id=?
            """, (date, group_id)).fetchall()

        city_schedules = {}
        rows = []
        for child in roster:
            city = CITIES_BY_ID.get(child["city_id"])
            record = row_day(child) if child["date"] is not None else blank_day(date)
            if city is None:
                day = snapshot(record)
            else:
                if city["id"] not in city_schedules:
                    today = instant.astimezone(ZoneInfo(city["timezone"])).date().isoformat()
                    if date != today:
                        schedule = {"available": False, "timezone": city["timezone"], "today": today,
                                    "times": None, "eligible_prayers": list(PRAYER_KEYS) if date < today else [],
                                    "error": "المواقيت متاحة عند فتح اليوم"}
                    else:
                        schedule = {**schedules.schedule(city, date), "today": today}
                        schedule["eligible_prayers"] = (
                            [key for key in PRAYER_KEYS if datetime.fromisoformat(schedule["times"][key]) <= instant]
                            if schedule["available"] else None
                        )
                    city_schedules[city["id"]] = schedule
                day = snapshot(record, city_schedules[city["id"]], scheduled=True)
            rows.append({
                "member": {"id": child["id"], "alias": child["alias"], "display_name": child["display_name"], "city_id": child["city_id"],
                            "timezone": city["timezone"] if city else None},
                "has_record": child["date"] is not None, "day": day,
            })

        # Schedule work runs outside a transaction; verify current grants before paging.
        with database.connect() as conn:
            require_group(conn, group_id, user, records=True)
            authorized = {row["user_id"] for row in conn.execute(
                '''SELECT target.user_id FROM group_members target
                   JOIN groups ON groups.id=target.group_id
                   JOIN group_members viewer ON viewer.group_id=groups.id
                   WHERE groups.id=? AND viewer.user_id=?
                     AND (groups.admin_id=viewer.user_id OR groups.members_can_view_records=1)''',
                (group_id, user['id']))}
        rows = [row for row in rows if row["member"]["id"] in authorized]
        aliases = {child["id"]: child["alias_key"] for child in roster}

        def order(row):
            stats = row["day"]["stats"]
            unknown = stats["completed"] is None
            return (unknown, -(stats["completed"] or 0), -(stats["silver_medals"] or 0),
                    aliases[row["member"]["id"]], row["member"]["id"])

        rows.sort(key=order)
        return {"date": date, "generated_at": instant.isoformat().replace("+00:00", "Z"),
                "limit": limit, "offset": offset, "total_members": len(rows),
                "rows": rows[offset:offset + limit]}

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
