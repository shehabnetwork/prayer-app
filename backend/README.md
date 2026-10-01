# Prayer tracker API

This local prototype uses aliases and passwords only. It does not request email,
birth dates, precise device location, or a child's identity. An account can save a selected city for prayer times. Use fictitious accounts for evaluation.
Do not put real children's data into this prototype without a separate privacy,
parental-consent, safeguarding, and deployment review.

## Run

From the `prayer-app` directory:

```sh
python -m pip install -r backend/requirements.txt
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/`. The same app serves `frontend/`, so a separate
frontend server and CORS configuration are unnecessary. API documentation is at
`/api/docs`; the OpenAPI schema is at `/openapi.json`.

Run the integration tests, using temporary databases and fictitious aliases:

```sh
python -m unittest discover -s backend/tests -v
```

The requirements are pinned to the versions used for verification. No database
seed or default account exists. Default persistence is
`backend/data/prayer.sqlite3`; it and its journal files are excluded from Git.

Configuration:

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `PRAYER_DB_PATH` | `backend/data/prayer.sqlite3` | SQLite file path |
| `PRAYER_COOKIE_SECURE` | `false` | Set `true` when serving through HTTPS |
| `PRAYER_TRUSTED_ORIGIN` | Request origin | Exact external origin, e.g. `https://example.com`, for reverse-proxy deployments |

`create_app(db_path, cookie_secure=..., trusted_origin=..., auth_limit=...)` is
available for integration tests or embedding. Pass a file path, not `:memory:`.
Deploy behind HTTPS, use secure cookies, restrict documentation if necessary,
protect the database/backups, and configure a trusted external origin before
production use. This small in-memory authentication limiter is per process; a
multi-worker/public deployment needs an appropriate shared abuse-control layer.

## Authentication

All routes below use the prefix `/api/v1`.

| Method / path | Body | Result |
| --- | --- | --- |
| `POST /auth/register` | `{ "alias": "demo-star", "password": "a-long-test-password" }` | `201`: authenticated session |
| `POST /auth/login` | Same credentials | `200`: authenticated session |
| `GET /auth/me` | None | `{ "user": { "id": 1, "alias": "demo-star" } }` |
| `POST /auth/logout` | None | `{ "ok": true }`; revoke the current session |
| `GET /health` | None | `{ "ok": true }`; public, contains no account data |

Registration/login responses contain:

```json
{"user":{"id":1,"alias":"demo-star"},"access_token":"opaque-token","token_type":"bearer"}
```

Aliases are 3–24 normalized characters: letters, digits, combining marks,
spaces, underscores, and hyphens. Alias matching is case-insensitive. Passwords
are 8–128 characters, never whitespace-only. No extra credential fields are
accepted. Passwords use PBKDF2-HMAC-SHA256 with a per-password random salt and
600,000 iterations. Only SHA-256 hashes of secure random session tokens are
stored. Sessions expire after 30 days.

The browser uses the HttpOnly, SameSite=Lax session cookie, with same-origin
requests. JavaScript should neither save nor use the returned bearer token.
Cookie-authenticated writes require a matching `Origin` header. The supplied
frontend uses ordinary same-origin fetch requests, which send this header.
Foreign origins are rejected, and no cross-origin CORS access is enabled.

A future native/mobile client can use `Authorization: Bearer <access_token>`.
Such requests do not need an Origin header. Keep the token in the operating
system's secure storage, never in logs. Logout invalidates that exact token,
including its cookie and bearer uses; other separately created sessions stay
signed in. IDs never let one account read or mutate another account's records.

## Day snapshot

`GET /days/YYYY-MM-DD` returns the following. A missing day is a blank snapshot;
reading it does not insert a record or alter statistics.

```json
{
  "date": "2026-09-30",
  "prayers": {"fajr":0,"dhuhr":0,"asr":0,"maghrib":0,"isha":0},
  "sunnah": {"fajr_before":0,"dhuhr_before":0,"dhuhr_after":0,"maghrib_after":0,"isha_after":0,"witr":false},
  "dhuhr_before_blocks": [false,false],
  "version": 0,
  "stats": {"completed":0,"total":5,"home":0,"mosque":0,"sunnah_rakahs":0,"witr":false,"gold_medals":0,"silver_medals":0,"percent":0}
}
```

Fard states are `0` unrecorded, `1` at home, and `2` at a mosque.
They cycle `0 -> 1 -> 2 -> 0`. Each completed fard counts once, regardless of
location. Past-day completion is out of the five fard prayers. For an account with a selected city, today counts only prayers whose scheduled time has arrived in that city; future prayers are disabled and do not contribute to earned medals or targets. Sunnah rakahs
and witr never affect this denominator or the fard completion percentage.

Rewards are derived from the current day record. A fard at home is worth one
gold medal; a fard at a mosque is worth 27 gold medals total. An unrecorded fard
is worth zero. Each completed Sunnah control is worth one silver medal, including
witr and each of the two independent Dhuhr-before blocks. Because rewards are
calculated from records, changing a state removes the old reward before applying
the new one and never creates duplicate awards.

Every successful mutation returns the full updated snapshot and increments
`version`. Integer/boolean fields use strict types: `true` is not the integer
`1`, and numeric strings are rejected. Unknown fields are rejected.

| Method / path | JSON body |
| --- | --- |
| `POST /days/{date}/prayers/{key}/cycle` | Optional `{ "version": 0 }`, `{}`, or no body |
| `PATCH /days/{date}/prayers/{key}` | `{ "state": 1, "version": 0 }` (version optional) |
| `PATCH /days/{date}/sunnah` | `{ "key": "fajr_before", "value": 2, "version": 0 }` (version optional) |
| `PATCH /days/{date}/sunnah` | `{ "key": "dhuhr_before", "block": 1, "value": true }` |
| `PATCH /days/{date}/sunnah` | `{ "key": "witr", "value": true }` |
| `PUT /days/{date}` | Full `{prayers,sunnah,dhuhr_before_blocks,version}`; version required |

The four non-Dhuhr-before sunnah keys accept `0` or `2`. Witr accepts a boolean.
Dhuhr-before contains two independent two-rakah blocks, numbered `0` and `1`.
Its patch requires a block and a boolean. The backend derives
`sunnah.dhuhr_before = 2 * checked_block_count`, yielding `0`, `2`, or `4`.
For example, setting only block 1 gives `[false,true]`, preserved across reloads
and database restarts. The full PUT must include both blocks and their matching
derived sunnah count; inconsistent snapshots are rejected.

Updates are serialized in SQLite transactions, so independent atomic changes
and fast prayer cycles do not lose updates. Supplying a stale version returns
`409` without making any change. A client using full PUT must re-read and resolve
the conflict; it must not silently retry with a new version.

## History and statistics

- `GET /history?from=2026-09-01&to=2026-09-30` returns
  `{ "from": "2026-09-01", "to": "2026-09-30", "days": [snapshots] }`
- Both history dates are required; the inclusive interval is at most 366 days
- Only recorded days are returned, in ascending date order
- `GET /statistics` includes all recorded days; optional `from` and `to` must
  be supplied together and obey the same range limit
- Dates must use strict ISO `YYYY-MM-DD` and must be actual calendar dates

Statistics result:

```json
{
  "recorded_days":0,"completed_days":0,"completed":0,"total":0,
  "home":0,"mosque":0,"sunnah_rakahs":0,"witr_days":0,
  "gold_medals":0,"silver_medals":0,"percent":0,
  "achievements":[{"key":"first_prayer","title":"بداية جميلة","description":"سجّلت أول صلاة، خطوة جميلة!","unlocked":false,"progress":0,"target":1}]
}
```

`total` sums eligible prayers across recorded days: five for past days and only elapsed prayers for today when a city is selected. `percent` is the rounded aggregate fard percentage
(zero with no recorded days). `completed_days` counts days with all five fard
prayers recorded. Achievements are `first_prayer`, `full_day`, `five_full_days`
(five complete days, not necessarily consecutive), and `sunnah_start` (sunnah or
witr recorded). The example above abbreviates the achievement array. There are
no punitive streaks, missed-day penalties, leaderboards, or comparisons to other
children. Since records are editable, aggregate achievements reflect the
currently stored records rather than irreversible awards.
The all-time `gold_medals` and `silver_medals` totals use the same derived reward
rules and are scoped to the authenticated account (and to the requested date
range when `from`/`to` are supplied).

## Errors and safeguards

API failures use `{ "detail": "helpful Arabic text" }`: `401` for missing or
invalid sessions/credentials, `403` for unsafe origins, `409` for duplicate
aliases or stale versions, `422` for invalid inputs, and `429` for too many auth
attempts (with `Retry-After`). Passwords and raw submitted validation values are
never included in error responses. The default limiter permits 20 authentication
attempts per client IP in 10 minutes.

Responses add anti-framing, no-sniff, restrictive browser-permission and content
security headers. API responses are marked `Cache-Control: no-store`. The CSP
permits local scripts, local assets, data images, and inline styles; use external
local JavaScript modules rather than inline scripts.

## City and prayer schedule

`GET /cities` lists the supported city catalog. `PATCH /account` with `{ "city_id": "jeddah" }` saves the city for the authenticated account. User responses include the city and its IANA timezone. Existing accounts start without a selected city and retain legacy totals until selection.

Day snapshots include schedule metadata and ISO prayer timestamps. Waiting is a derived display state, not a fourth stored integer. Today uses elapsed prayer times for `total`, `percent`, `gold_target`, and `silver_target`; past days retain 5/135/7. Silver target weights are Fajr 1, Dhuhr 3, Asr 0, Maghrib 1, and Isha 2 (including Witr). All mutation routes enforce schedule eligibility, including bulk updates.

Schedules come from the [AlAdhan prayer times API](https://aladhan.com/prayer-times-api), with regional calculation methods. Only the selected city’s catalog coordinates, date, timezone, and calculation method are sent to the provider. Provider failures must not invent times or completion percentages; today’s gated controls remain unavailable until a schedule is obtained. Tests mock the clock and provider instead of making live requests.
