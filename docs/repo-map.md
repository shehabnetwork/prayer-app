# Repository map

Current implementation reference, updated October 4, 2026. Inspected base commit:
`150525c` plus the group implementation in the working tree on `codex/groups`.
Source changes described below have been verified by SQLite, real PostgreSQL,
and Node integration tests; browser rendering has not been verified.

Start with the feature table below, then inspect the relevant source before
editing. Source code is authoritative. Update affected sections after every new
feature or change that makes this map inaccurate; keep implementation history
and detailed validation in `docs/task-notes/`.

## Application structure and data flow

The application is an Arabic RTL prayer tracker with alias/password accounts,
per-account diaries, city-based prayer times, achievements, and named groups.
Any account can create groups and become their admin. Admins read member records;
the optional shared-visibility setting lets all current members read one another's
records, including the admin's. Personal diaries remain owner-editable.

`frontend/index.html` loads `frontend/app.js` and `frontend/styles.css`.
`app.js` renders views, handles events, and calls the same-origin FastAPI API.
`frontend/domain.js` contains pure prayer, reward, date, and period calculations.
`frontend/assets/` contains prayer-state illustrations and other presentation assets.

`backend/main.py:create_app` constructs FastAPI, registers `/api/v1` routes, and
serves `frontend/` at `/`. Uvicorn entry point: `backend.main:app`.
Routes use `backend/database.py:Database` for persistence, while some account and
group SQL lives directly in route handlers. `backend/schedule.py:ScheduleService`
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
| Registration, login, sessions | `Credentials`, `Registration`, `normalize_alias`, `normalize_display_name`, `current_user`; `/auth/register`, `/auth/login`, `/auth/me`, `/auth/logout`. UI authentication forms and submit handler in `app.js`. `alias` remains the unique login username; independent, nonunique `display_name` appears in greetings and social identities. Registration collects both; older API clients may omit display name and inherit alias. PBKDF2 password hashes; only session-token hashes stored. HttpOnly SameSite cookie and bearer authentication; cookie writes check Origin. Authentication limiter is in memory per process. | `backend/tests/test_api.py`, `tests/frontend_integration.test.mjs` |
| Account settings and city selection | `AccountPatch`, `/account`, `/cities`; UI `loadCities`, account form. `users.display_name` is editable through `/account` and the display-name form; names are trimmed, 1–80 characters and reject control characters. `users.city_id` stores a curated city; deprecated `can_supervise` and registration `account_type` remain for old clients/data but never grant group permissions. No device geolocation. | `backend/tests/test_prayer_times.py`, API and frontend integration tests |
| Prayer diary and day navigation | `Prayers`, `Sunnah`, `DayPut`, `PrayerPatch`, `CyclePatch`, `SunnahPatch`; `/days/{date}` and prayer/sunnah mutations. UI `loadDay`, `render`, event handlers; domain `PRAYERS`, `STATES`, `nextState`, `emptyDay`, `toggleDhuhrBlocks`. Fard cycles unrecorded → home → mosque; Dhuhr-before has two independent blocks; Witr is independent. Read of a missing day returns a blank day without inserting it. | `backend/tests/test_api.py`, `tests/domain.test.mjs`, `tests/frontend_integration.test.mjs` |
| Safe concurrent edits | `Database.update_day` locks before read-modify-write, increments `version`, and rejects a supplied stale version with HTTP 409. `Connection.lock_users` uses SQLite writer locking or ordered PostgreSQL owner-row locks. Frontend request guards prevent delayed responses from replacing another account/day's state. | API and frontend integration tests; PostgreSQL races in `backend/tests/test_postgres.py` |
| Prayer times and eligibility | `backend/schedule.py:CITIES`, `CITIES_BY_ID`, `ScheduleService.timings`/`schedule`; backend `schedule_for`, `eligible_for_mutation`, `selected_snapshot`, `statistical_snapshot`, `history_snapshot`; domain `eligiblePrayerKeys`, `dateInTimezone`. AlAdhan receives city coordinates/date/method, not account data. Today's future prayers and attached Sunnah controls are disabled; unavailable schedules do not produce a fabricated completion percentage. Past days count all five prayers. | `backend/tests/test_prayer_times.py`, `tests/domain.test.mjs`, frontend integration tests |
| Rewards and achievements | Backend `day_stats`, `snapshot`, `aggregate`, `/statistics`; frontend `loadAchievements`; domain `dailyStats`, `achievementRange`. Home fard earns 1 gold, mosque fard 27; each completed Sunnah control earns 1 silver (including each Dhuhr-before block and Witr). Rewards are derived from records. Periods are Saturday–Friday week, calendar month, or last 30 days; today's targets reflect elapsed prayers. | `backend/tests/test_api.py`, `backend/tests/test_prayer_times.py`, `tests/domain.test.mjs`, frontend integration tests |
| History and compact record tables | Own `/history` and group member history routes; frontend `loadAchievements`, `compactPrayerTable`. Shared compact presentation covers achievement history, read-only member day and group daily report; own-day board remains editable. `frontend/styles.css` aligns proportional prayer columns and RTL slots. | `tests/frontend_integration.test.mjs`, `tests/groups_integration.test.mjs`, `tests/daily_review_integration.test.mjs` |
| Groups, settings and memberships | `GroupCreate`, `GroupPatch`, `group_view`, `require_group`; `/groups` create/list, `/groups/{id}` detail/settings/delete, `/membership` leave and `/members/{id}` remove. Creator is admin and member; private by default; admin cannot leave/remove self. Admin-only `DELETE /groups/{id}` locks the group and atomically deletes acceptance receipts, invitations, memberships and the group; users and personal prayer records remain intact. Frontend `groupManagement`, `groupsScreen`, `loadManagement`, `selectGroup`, `groupAction`; Arabic groups navigation replaces account-role gating. Selected group uses a compact RTL blue/navy header (outline icons, inline name edit, immediate admin-only sharing switch, split daily-report/copy controls) and initials-avatar member rows with admin badges/read buttons; a top-left button group contains New group and refresh, and New opens an on-demand creation dialog. Lower management panels are absent; removal/leave use compact row/header controls. The groups panel fills the shared responsive main-content width. A red admin header delete icon opens a confirmation dialog with space below its message, a red delete action on the right and cancel on the left; successful deletion clears selected member/report caches and reloads remaining groups. `groupIcon`, `memberInitials` and `groupEditing` support the presentation; transient dialog/edit state resets on group/session changes. | `backend/tests/test_groups.py`, `tests/groups_integration.test.mjs`, PostgreSQL parity suites |
| Group invitation links | `InviteToken`, `InviteAcceptance`, canonical invitation ensure helper, `invite_group_view`; creation inserts a durable invitation in the same transaction as the group. Admin-only, origin-protected `POST /groups/{id}/invite-link` retrieves it or ensures a missing canonical link under the group lock, preserving active legacy hash-only invitations. `group_invites.share_token` stores a nullable recoverable token alongside its lookup hash; one active canonical and one active legacy link per group. Tokens never appear in member metadata, preview, acceptance or record responses. Copy works after reload/login/group switches without explicit URL creation or automatic rotation. `/group-invites/preview` and `/accept` preserve explicit consent and receipt semantics; replay after leaving/removal returns 409 without regrant. Fragment links remain `#invite=TOKEN`. Explicit legacy administration endpoints remain available; deliberate rotation revokes all active links. | Group backend/Node tests and PostgreSQL acceptance-lock race test |
| Read-only group member diary | `group_member`, `member_day`, `member_history`, `member_statistics`; `/groups/{group_id}/members/{member_id}/days/{day}`, `/history`, `/statistics`. One SQL predicate checks requester/target memberships plus current admin/shared visibility (self reads also allowed), rechecked after record/schedule work. Frontend `readPath`, `selectChild`, `contextStamp` bind reads to group/member/session; errors and switches clear sensitive caches. Mutations remain owner-only. Legacy supervision URLs return 410 through middleware; no legacy grant fallback. | Group access-control tests, revocation during schedule work, Node stale-member/group/session requests |
| Daily group report | `/groups/{id}/daily-review`; admin or shared-group members only. Batch roster/day lookup includes admin; grants are rechecked after grouped city schedule work, then full roster sorted before paging. Response uses `total_members` and row `member`. Frontend `dailyReport`, `loadDailyReport`, `reportChild`, `changeReportDate`: yesterday default, 50-row pages, read-only member diary and return. Rank uses completed fard then Sunnah, not mosque reward; missing records remain blank. | `backend/tests/test_daily_review.py`, `tests/daily_review_integration.test.mjs`, PostgreSQL parity |
| PostgreSQL and SQLite persistence/migration | `backend/database.py:Database`, `Connection.lock_users`/`lock_group`, `_migrate_postgres`, `_migrate_groups_sqlite`; `backend/groups_schema.py:SQLITE_SCHEMA`, `migrate_legacy`; migrations `001_initial.sql`, `002_groups.sql`, `003_durable_group_links.sql`, `004_display_names.sql`. Existing users receive their alias as the initial display name; SQLite adds/backfills the column transactionally and PostgreSQL migration 004 enforces non-null names. PostgreSQL migrations are transactional/advisory-locked; SQLite group conversion uses `BEGIN IMMEDIATE` plus `app_migrations(groups_v1)`. One private group per legacy supervisor with relationships/invites preserves current membership, invite IDs/hashes/revocations/receipts. Inert legacy rows are never used for grants or reconverted after marker. SQLite `groups_v2` adds recoverable tokens before replacing invitation indexes; schema replay does not recreate the old unique-active index. | `backend/tests/test_groups.py`, `test_postgres.py`, snapshot/import tests |
| Existing SQLite data transfer | `backend/import_sqlite.py:TABLES`, `read_snapshot`, `import_sqlite`, CLI. Read-only source snapshot; old sources convert in memory, new complete group schemas remain authoritative even with stale legacy rows. Partial group schemas fail. Older user snapshots default missing `display_name` to alias; explicit names are preserved. Older complete group snapshots default missing `share_token` to null; present tokens are preserved and checked against their hashes. Locks empty target, copies ten application tables atomically, verifies exact values and resets identities for users/legacy invites/groups/group invites; initialized target retains migration ledgers. Dry run/failure rolls back. No automatic cutover or reverse import. | `backend/tests/test_postgres_import.py` |

## Storage and configuration

Active data lives in `users`, `sessions`, `days`, `groups`, `group_members`,
`group_invites`, and `group_invite_acceptances`. Legacy `supervisor_children`,
`supervisor_invites`, and `supervisor_invite_acceptances` remain inert migration
input/audit data. Both engines retain `app_migrations(groups_v1,groups_v2)`; PostgreSQL
also tracks numbered SQL versions in `schema_migrations`. Group visibility is a
0/1 flag; `(group_id,user_id)` membership and `(user_id,request_key)` receipt keys
are unique. Canonical invitations retain a recoverable capability in `share_token`; database/backups therefore contain usable invitation secrets. Nullable tokens distinguish legacy links. Group mutations lock the group; acceptance also locks the user in
PostgreSQL, serializing request keys across groups.
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

Latest group implementation validation (October 4): real PostgreSQL and SQLite
backend suites passed against isolated temporary test schemas; one
SQLite-specific conversion fixture is skipped by the PostgreSQL parity subclass.
Node suites exercise the real frontend handlers/API through a small DOM adapter,
including group privacy, invitation consent, member/group/session stale reads,
daily reports and existing diary regressions. See [group task notes](task-notes/groups.md)
for final counts/commands. Tests used the configured local PostgreSQL server,
with application PostgreSQL/SQLite data left untouched. Browser/visual rendering
and production deployment remain unverified.

## Further references

- [AGENTS.md](../AGENTS.md): discovery, coordination and map maintenance rules.
- [Backend README](../backend/README.md): API contracts, authentication and settings.
- [Project README](../README.md): Arabic usage and local setup; historical test totals
  there may predate the latest validation above.
- [Groups](task-notes/groups.md) describes the current permissions and migration.
- Historical [supervisor links](task-notes/supervisor-links.md), [daily review](task-notes/daily-review.md),
  [compact record views](task-notes/compact-record-views.md), and
  [PostgreSQL migration](task-notes/postgresql-migration.md): implementation decisions
  and detailed validation records.
- [PUBLIC-PREVIEW.md](PUBLIC-PREVIEW.md): historical public preview instructions;
  hosting state and public URLs have not been verified in this refresh.
- README mentions a separate `prayer-cloud` Worker/D1 application; it is outside
  this repository map and was not inspected.
