# PostgreSQL migration — October 4, 2026

Objective: migrate persistence on branch codex/postgresql-migration, preserving current API/UI and data semantics. Base inspected: 1a891f586165e2875e199f9093a4c122bbc24ae9; working tree initially clean. Navigation: docs/repo-map.md.

Findings: Database in backend/main.py owns schema/connection/day updates, but route SQL also accesses connections. SQLite-specific points include ? placeholders, named/positional rows, lastrowid, IntegrityError, PRAGMAs, BEGIN IMMEDIATE and INSERT OR IGNORE. Tests inject temporary paths and some inspect rows or SQLite migration fixtures.

Acceptance: PostgreSQL configured by URL without silent fallback; versioned schema migration; existing hashes, IDs, sessions, cities, records, invite metadata/receipts transferable from read-only SQLite; target import atomic/nonoverwriting; row/concurrency semantics preserved for day edits and invite acceptance/revocation. Real PostgreSQL tests required in addition to legacy regression. Frontend/UI untouched.

Tools: local full PostgreSQL16 binaries at /opt/homebrew/opt/postgresql@16/bin; isolated temporary cluster created for validation (not user's application DB). psycopg[binary]3.2.13 installed for development. No browser/desktop use.

Status: implementation complete on codex/postgresql-migration. Backend worker owned main/storage/migrations/requirements; transfer worker owned importer/tests; main owned parity tests, docs and isolated validation database. No application SQLite data was transferred or deleted.

Implementation: extracted storage adapter; PostgreSQL URL configuration, ordered user locks with FOR NO KEY UPDATE, repeatable versioned migration advisory lock, sanitized connection errors. Explicit SQLite paths and local default remain supported; configured bad PostgreSQL URLs fail rather than fall back. Importer preserves six tables with read-only source snapshot, empty-target locks, exact row verification, transactional identity resets and dry-run.

Review: Astra supplied locking architecture and final risk review. Fixed importer default-schema mismatch and malformed-URL credential disclosure; added conflicting-search-path and sanitized-error regressions. Deterministic blocked acceptance tests verify revocation recheck and retry-after-unlink safety; reciprocal supervision avoids lock-order deadlock. No remaining blocker reported.

Validation: final combined SQLite/PostgreSQL run recorded below; JavaScript suite13/13 passed, and daily report frontend integration also passed with a real PostgreSQL-backed API. npm run check, git diff --check and optional Compose configuration validation passed. Migration CLI validated against an isolated schema. PostgreSQL tests use random schemas in a dedicated temporary PostgreSQL16 cluster. Legacy SQLite fixture tests are explicitly skipped only in the PostgreSQL parity subclass and remain covered by SQLite/import tests.

Limitations: existing SQLite installation remains until explicit cutover using docs/POSTGRESQL-MIGRATION.md; no production deployment, browser testing, shared rate limiter or reverse import. Connection-per-operation storage retains original architecture; owner locks serialize dates for one user. Import holds snapshot rows in memory and requires initialized empty target and write outage. Token Guard virtualenv/Node execution unsupported; ordinary runners supplied test results (not measured output).

Final Python suite: 98 tests, 96 passed, 2 expected PostgreSQL-subclass skips for SQLite legacy fixtures. All 40 original SQLite tests passed. CLI smoke verified schema initialization, import dry-run and actual fixture import into an isolated empty PostgreSQL schema. Final review reported no remaining blockers.
