# PostgreSQL setup and SQLite cutover

PostgreSQL is supported for deployment; SQLite remains available for existing local installations and legacy tests. No existing SQLite database is automatically replaced or deleted.

## Configuration

- `PRAYER_DATABASE_URL`: PostgreSQL URL, e.g. `postgresql://user:password@host:5432/prayer`. Configured malformed/unreachable URLs fail startup; they do not fall back to SQLite. Do not commit credentials.
- `PRAYER_DATABASE_SCHEMA`: optional isolated schema (default `public`), useful for tests and staging.
- `PRAYER_DB_PATH`: legacy SQLite path, used when no PostgreSQL URL is configured.
- Explicit `create_app(path_or_url)` takes precedence over environment selection for embedding/tests.

The production driver is Psycopg 3. Application schema changes are versioned in `backend/migrations/`; migration startup is transactional and serialized by an advisory lock. Unknown newer versions are rejected. Dates and day JSON remain text, and timestamp values remain epoch seconds, preserving existing API/data semantics.

## Local PostgreSQL

Install updated Python dependencies:

```sh
source .venv/bin/activate
python -m pip install -r backend/requirements.txt
```

Use your PostgreSQL server, or the optional Compose service (requires Docker):

```sh
export PRAYER_POSTGRES_PASSWORD="$(python -c 'import secrets; print(secrets.token_hex(24))')"
docker compose -f compose.postgres.yml up -d
export PRAYER_DATABASE_URL="postgresql://prayer:${PRAYER_POSTGRES_PASSWORD}@127.0.0.1:15432/prayer"
python -m backend.database migrate
```

The Compose port binds to localhost and uses a persistent named volume. Record the generated password securely: changing `POSTGRES_PASSWORD` does not reset the password of an already initialized PostgreSQL volume. Avoid deleting the volume unless intentionally discarding its data.

For a fresh empty installation, start the app:

```sh
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

For existing data, complete the transfer below before allowing account creation.

## Transfer existing SQLite data

1. Stop application writers and keep the old SQLite database as a rollback source. Keep a separate consistent backup (SQLite backup API includes committed WAL contents).
2. Configure an empty PostgreSQL target and run `python -m backend.database migrate`.
3. Validate the exact transfer without retaining rows:

```sh
python -m backend.import_sqlite --source backend/data/prayer.sqlite3 --dry-run
```

4. Perform the import:

```sh
python -m backend.import_sqlite --source backend/data/prayer.sqlite3
```

5. Start the application with `PRAYER_DATABASE_URL`. Verify login, existing sessions, prayer history, groups, invitation acceptance, record visibility, and the daily report before reopening writes.

The importer opens SQLite read-only and takes a consistent snapshot. It rejects unsupported/corrupt schema/data, locks the target tables, refuses any preexisting application rows, and copies the ten application tables (including inert legacy tables and four authoritative group tables) in foreign-key order within one PostgreSQL transaction. It compares complete values and row counts, preserving IDs, JSON text, hashes, receipt keys, and timestamps. Identity sequences for users, legacy invitations, groups and group invitations are reset for subsequent creation. The initialized target retains its migration ledgers. A failed import or dry run does not retain copied rows. Run during a write outage; a successful snapshot does not include SQLite writes made after it was captured.

Legacy SQLite databases without city/supervision columns receive the previous defaults. Legacy relationships and invitation hashes/receipts are converted into private groups in the read-only in-memory snapshot. Already migrated sources must contain all four group tables and the `groups_v1` marker in `app_migrations`; group data remains authoritative even when inert legacy rows disagree. Partial group schemas are rejected. Complete older group snapshots without `share_token` remain supported with null defaults; newer snapshots preserve recoverable tokens and validate their lookup hashes. Migration `003_durable_group_links.sql` and SQLite marker `groups_v2` add canonical link storage without rotating existing invitations. Database backups now contain usable invitation tokens and must retain the same access protections as the database. Startup runs PostgreSQL migration `002_groups.sql` or the transactional SQLite `groups_v1` conversion exactly once, so leaving a group is not undone on restart. Password resets are unnecessary; stored hashes remain identical. No raw password or invitation secret is exported.

Rollback before reopening writes: stop the app, remove `PRAYER_DATABASE_URL`, and restore `PRAYER_DB_PATH` to the preserved SQLite source. After PostgreSQL receives new writes, reverting would lose them; there is no automatic reverse import or dual-write mechanism.

## Verification

Use a dedicated test database. Tests create/drop randomly named schemas and must not target a production database:

```sh
export PRAYER_TEST_DATABASE_URL='postgresql://test_user:password@127.0.0.1:5432/prayer_test'
python -m unittest discover -s backend/tests -v
PYTHON="$PWD/.venv/bin/python" npm test
npm run check
```

The PostgreSQL suite reuses existing API, groups, schedule, and report tests and adds first-day/version races, retry receipts, invite revocation races and group visibility checks, and importer checks. SQLite-specific legacy fixtures remain covered by the SQLite suite. Without the test URL, PostgreSQL tests are explicitly skipped, not claimed as passing.

## Deployment notes

Use a persistent managed PostgreSQL database or a separately backed-up database service, HTTPS, secure cookies, and `PRAYER_TRUSTED_ORIGIN` for the external origin. Require TLS to a remote database where supported (e.g. the provider's required `sslmode`). Use restricted database credentials and keep connection URLs out of logs.

PostgreSQL transactions use consistent user-row locks to serialize conflicting application operations. Day updates lock the owner; group membership, settings and invitation changes lock the group row. Acceptance additionally locks the accepting user to serialize request keys across groups. Record authorization checks both memberships and visibility together and rechecks grants after external schedule work. The current in-memory rate limiter is still per process: database migration alone does not make it shared across workers.
