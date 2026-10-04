# Repository map

Current implementation reference, updated October 4, 2026. Inspected commit:
`729097aab4c3d31e64435967fdc32a6c512cbf3d` (PostgreSQL storage and SQLite migration).
The working tree was clean before this documentation refresh.

Start with the feature table below, then inspect the relevant source before
editing. Source code is authoritative. Update affected sections after every new
feature or change that makes this map inaccurate; keep implementation history
and detailed validation in `docs/task-notes/`.

## Application structure and data flow

The application is an Arabic RTL prayer tracker with alias/password accounts,
per-account diaries, city-based prayer times, achievements, and shared supervisor
invitations. Supervisors retain their own diary and can read linked students' records.

`frontend/index.html` loads `frontend/app.js` and `frontend/styles.css`.
`app.js` renders views, handles events, and calls the same-origin FastAPI API.
`frontend/domain.js` contains pure prayer, reward, date, and period calculations.
`frontend/assets/` contains prayer-state illustrations and other presentation assets.

`backend/main.py:create_app` constructs FastAPI, registers `/api/v1` routes, and
serves `frontend/` at `/`. Uvicorn entry point: `backend.main:app`.
Routes use `backend/database.py:Database` for persistence, while some account and
supervision SQL lives directly in route handlers. `backend/schedule.py:ScheduleService`
fetches and caches AlAdhan timings for curated cities. There is no separate
frontend build or frontend server.

Data flow: UI event → API authorization/validation → database transaction → day
snapshot or report → UI render. Schedule-dependent responses also use the selected
city's timezone and timings. Browser requests use the session cookie; invitation
secrets enter through the URL fragment and are submitted in request bodies.

## Feature implementation index

Backend symbols below are in `backend/main.py` unless another file is named.
Frontend UI symbols are in `frontend/app.js`; pure calculation symbols are in
`frontend/domain.js`.

| Feature | Implementation and important behavior | Tests |
| --- | --- | --- |
| Registration, login, sessions | `Credentials`, `Registration`, `normalize_alias`, `current_user`; `/auth/register`, `/auth/login`, `/auth/me`, `/auth/logout`. UI authentication forms and submit handler in `app.js`. PBKDF2 password hashes; only session-token hashes stored. HttpOnly SameSite cookie and bearer authentication; cookie writes check Origin. Authentication limiter is in memory per process. | `backend/tests/test_api.py`, `tests/frontend_integration.test.mjs` |
| Account settings and city selection | `AccountPatch`, `/account`, `/cities`; UI `loadCities`, account form/change handler. `users.city_id` stores a curated city, and `users.can_supervise` enables supervision. No device geolocation. | `backend/tests/test_prayer_times.py`, `backend/tests/test_supervisors.py`, frontend integration tests |
| Prayer diary and day navigation | `Prayers`, `Sunnah`, `DayPut`, `PrayerPatch`, `CyclePatch`, `SunnahPatch`; `/days/{date}` and prayer/sunnah mutations. UI `loadDay`, `render`, event handlers; domain `PRAYERS`, `STATES`, `nextState`, `emptyDay`, `toggleDhuhrBlocks`. Fard cycles unrecorded → home → mosque; Dhuhr-before has two independent blocks; Witr is independent. Read of a missing day returns a blank day without inserting it. | `backend/tests/test_api.py`, `tests/domain.test.mjs`, `tests/frontend_integration.test.mjs` |
| Safe concurrent edits | `Database.update_day` locks before read-modify-write, increments `version`, and rejects a supplied stale version with HTTP 409. `Connection.lock_users` uses SQLite writer locking or ordered PostgreSQL owner-row locks. Frontend request guards prevent delayed responses from replacing another account/day's state. | API and frontend integration tests; PostgreSQL races in `backend/tests/test_postgres.py` |
| Prayer times and eligibility | `backend/schedule.py:CITIES`, `CITIES_BY_ID`, `ScheduleService.timings`/`schedule`; backend `schedule_for`, `eligible_for_mutation`, `selected_snapshot`, `statistical_snapshot`, `history_snapshot`; domain `eligiblePrayerKeys`, `dateInTimezone`. AlAdhan receives city coordinates/date/method, not account data. Today's future prayers and attached Sunnah controls are disabled; unavailable schedules do not produce a fabricated completion percentage. Past days count all five prayers. | `backend/tests/test_prayer_times.py`, `tests/domain.test.mjs`, frontend integration tests |
| Rewards and achievements | Backend `day_stats`, `snapshot`, `aggregate`, `/statistics`; frontend `loadAchievements`; domain `dailyStats`, `achievementRange`. Home fard earns 1 gold, mosque fard 27; each completed Sunnah control earns 1 silver (including each Dhuhr-before block and Witr). Rewards are derived from records. Periods are Saturday–Friday week, calendar month, or last 30 days; today's targets reflect elapsed prayers. | `backend/tests/test_api.py`, `backend/tests/test_prayer_times.py`, `tests/domain.test.mjs`, frontend integration tests |
| History and compact record tables | `/history` and child history routes; frontend `loadAchievements`, `compactPrayerTable`. Shared compact presentation covers achievement history, selected student read-only day, and daily student report; editable own-day board remains a separate renderer. `frontend/styles.css` aligns proportional prayer columns and RTL icon slots. | `tests/frontend_integration.test.mjs`, `tests/supervisors_integration.test.mjs`, `tests/daily_review_integration.test.mjs` |
| Supervisor invitations and relationships | `InviteToken`, `InviteAcceptance`, supervisor invite create/rotate/list/revoke, `/supervisor-invites/preview` and `/accept`, `/account/supervisors`, `/supervisor/children`. Frontend `captureInvite`, `loadManagement`, `supervisionAction`, acceptance handlers. One active reusable link per supervisor; only its hash is stored. Explicit child acceptance creates a relationship; multiple supervisors are supported. Rotation/revocation blocks future acceptance without removing existing relationships. Receipt keys make retries safe, including after unlinking. | `backend/tests/test_supervisors.py`, `tests/supervisors_integration.test.mjs`, PostgreSQL concurrency tests |
| Read-only student diary | Supervisor child day/history/statistics routes recheck the current relationship before returning records. Frontend `loadDay`/view state selects the child and prevents edits. Supervisors can remove students; students can remove supervisors. | `backend/tests/test_supervisors.py`, `tests/supervisors_integration.test.mjs` |
| Daily supervisor report | `/supervisor/daily-review`; batch roster/day lookup, relationship recheck, grouped city schedules, ordering before pagination. Frontend `dailyReport`, `loadDailyReport`, `changeReportDate`; yesterday default, date/page navigation, up to 50 students per page, read-only student diary and return. Sort by completed fard, then completed Sunnah; mosque reward does not inflate completion rank. Missing records remain blank. | `backend/tests/test_daily_review.py`, `tests/daily_review_integration.test.mjs` |
| PostgreSQL and SQLite persistence | `backend/database.py:Database`, `Connection`, `Row`, `postgres_parameters`, `_migrate_postgres`, `get_day`, `update_day`, `get_days`; `backend/migrations/001_initial.sql`. PostgreSQL uses Psycopg 3, versioned transactional migrations and an advisory migration lock; unknown newer versions fail. SQLite remains the local default, with WAL and legacy schema upgrades. | `backend/tests/test_postgres.py` plus SQLite API/supervision/report suites |
| Existing SQLite data transfer | `backend/import_sqlite.py:read_snapshot`, `import_sqlite`, CLI `main`. Read-only source snapshot; validation, empty-target locks, six-table atomic copy, exact value verification, identity reset and rollback on dry run/failure. Preserves IDs, hashes, sessions, records, relationships and invite receipts. No automatic cutover or reverse import. | `backend/tests/test_postgres_import.py` |

## Storage and configuration

The six application tables are `users`, `sessions`, `days`,
`supervisor_children`, `supervisor_invites`, and `supervisor_invite_acceptances`.
PostgreSQL additionally tracks applied versions in `schema_migrations`.
Day prayer/Sunnah/block values are JSON text; dates are ISO date text and
stored timestamps are epoch seconds. The storage adapter preserves existing
API semantics across both engines.

| Setting | Meaning |
| --- | --- |
| `PRAYER_DATABASE_URL` | PostgreSQL URL. Takes precedence over the environment SQLite path; invalid/unreachable configured targets fail rather than silently falling back. |
| `PRAYER_DATABASE_SCHEMA` | PostgreSQL schema, default `public`; useful for isolated staging/test schemas. |
| `PRAYER_DB_PATH` | SQLite path if no PostgreSQL URL is set; default `backend/data/prayer.sqlite3`. |
| `PRAYER_COOKIE_SECURE` | Default false; enable for HTTPS deployments. |
| `PRAYER_TRUSTED_ORIGIN` | Exact external origin for cookie-write checks behind a proxy; default uses request origin. |
| `PRAYER_TEST_DATABASE_URL` | Dedicated PostgreSQL test target; tests create/drop random schemas. Unset means PostgreSQL tests are skipped. |

An explicit database path/URL supplied to `create_app` overrides environment
selection. SQLite database files are excluded from Git. There is no seed/default
account. Each database operation opens a connection; this implementation does
not introduce a connection pool or a shared multi-process authentication limiter.

An already installed PostgreSQL server can be used directly through
`PRAYER_DATABASE_URL`; Docker is optional. `compose.postgres.yml` offers PostgreSQL
16 with a persistent volume and localhost port `15432`. Native PostgreSQL's port
comes from the local server configuration. Setup, migration, transfer and rollback
instructions live in [POSTGRESQL-MIGRATION.md](POSTGRESQL-MIGRATION.md).

Local testing configuration (October 4): Git-ignored `.env.local` selects
`localhost:5432`, user `postgres`, database `prayer_app_local`, schema `public`.
Credentials stay in that local file. The database was created and the migration
CLI completed successfully. This is a fresh database; existing SQLite data was
not imported. To load these settings and start the app from the repository root:

```sh
set -a
source .env.local
set +a
.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

## Run and verify

### VPS publishing

`scripts/publish.sh` uploads a bounded backend/frontend source archive through
OpenSSH to `administrator@mafisco.com` by default. `scripts/deploy-remote.sh`
targets Linux/systemd under `~/www/prayer-app`: a locked deployment creates a
per-release venv, runs `backend.database migrate`, atomically switches `current`,
restarts a single-worker loopback Uvicorn service, and checks `/api/v1/health`.
Failure after activation restores the prior code release; migrations are not
reversed. Secrets live in mode-600 `shared/prayer-app.env`, outside releases.
First setup requires explicit HTTPS origin or `--preview` for a localhost SSH
tunnel. The SSH hostname does not select a public hostname. The selected public
hostname is `praytracker.freedynamicdns.org`. Optional `--nginx-host` runs
`scripts/configure-nginx-remote.sh` after deployment: dedicated Nginx host,
interactive Certbot webroot issuance, bounded HTTPS health verification (15
verified loopback attempts, three-second request timeout, one-second intervals), configuration
rollback, and a renewal reload hook. Existing sites remain preserved; DNS and
renewal scheduling are managed separately. See [DEPLOYMENT.md](DEPLOYMENT.md).
Final readiness failure reports curl/health errors without JSON tracebacks and
prints only loaded listener/host directives before rollback. Regression checks:
`python3 -m unittest discover -s tests -p test_nginx_readiness.py` (mocked commands).
Remote behavior remains unverified
until actual deployment. Local checks: `bash -n scripts/publish.sh
scripts/deploy-remote.sh scripts/configure-nginx-remote.sh`,
`scripts/publish.sh --help`, and `--dry-run`.

Run commands from the repository root with Python 3.12+; JavaScript tests require
Node 24+ according to README. The frontend has no npm dependencies or build step.

| Purpose | Command |
| --- | --- |
| Install backend dependencies | `python -m pip install -r backend/requirements.txt` |
| Serve API and frontend | `python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000` |
| Initialize/upgrade configured PostgreSQL | `python -m backend.database migrate` |
| Validate SQLite transfer | `python -m backend.import_sqlite --source backend/data/prayer.sqlite3 --dry-run` |
| Transfer SQLite data | `python -m backend.import_sqlite --source backend/data/prayer.sqlite3` |
| Python suite | `python -m unittest discover -s backend/tests -v` |
| JavaScript/domain/API integration suite | `PYTHON="$PWD/.venv/bin/python" npm test` |
| JavaScript syntax | `npm run check` |
| Patch whitespace | `git diff --check` |

`package.json` defines `npm test` as `node --test tests/*.test.mjs` and `npm run
check` as syntax checks of `frontend/app.js` and `frontend/domain.js`.
Frontend integration tests use a small DOM adapter and a real local API process;
they do not validate browser rendering. `tests/ui_smoke.py` is an optional
Playwright test, subject to the user's explicit browser authorization requirement.

Latest recorded implementation validation (October 4, see PostgreSQL task note):
98 Python tests, 96 passed and 2 expected SQLite-fixture skips in the PostgreSQL
parity subclass; all 40 original SQLite tests passed. JavaScript tests passed
13/13, with additional daily report integration against real PostgreSQL.
Migration/import CLI smoke checks, JavaScript syntax, whitespace and Compose
configuration checks also passed. PostgreSQL validation used an isolated temporary
PostgreSQL 16 cluster, not the user's application database. These are recorded
results, not tests rerun during this documentation refresh. No production cutover,
deployment or browser/visual verification is claimed.

## Further references

- [AGENTS.md](../AGENTS.md): discovery, coordination and map maintenance rules.
- [Backend README](../backend/README.md): API contracts, authentication and settings.
- [Project README](../README.md): Arabic usage and local setup; historical test totals
  there may predate the latest validation above.
- [Supervisor links](task-notes/supervisor-links.md), [daily review](task-notes/daily-review.md),
  [compact record views](task-notes/compact-record-views.md), and
  [PostgreSQL migration](task-notes/postgresql-migration.md): implementation decisions
  and detailed validation records.
- [PUBLIC-PREVIEW.md](PUBLIC-PREVIEW.md): historical public preview instructions;
  hosting state and public URLs have not been verified in this refresh.
- README mentions a separate `prayer-cloud` Worker/D1 application; it is outside
  this repository map and was not inspected.
