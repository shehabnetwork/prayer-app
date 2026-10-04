"""SQLite development storage and PostgreSQL production storage."""
from __future__ import annotations
from contextlib import contextmanager
import os
from pathlib import Path
import re
import sqlite3
import time
import json
from fastapi import HTTPException


class Row(dict):
    """Mapping rows with SQLite-compatible positional access."""
    def __getitem__(self, key):
        return tuple(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


def postgres_parameters(query: str) -> str:
    """Translate markers outside SQL strings, identifiers and comments."""
    pieces = []
    i = 0
    while i < len(query):
        start = i
        if query[i] in "'\"":
            quote = query[i]
            i += 1
            while i < len(query):
                if query[i] == quote:
                    i += 1
                    if i < len(query) and query[i] == quote:
                        i += 1
                        continue
                    break
                i += 1
        elif query.startswith('--', i):
            end = query.find('\n', i)
            i = len(query) if end < 0 else end
        elif query.startswith('/*', i):
            depth = 1
            i += 2
            while i < len(query) and depth:
                if query.startswith('/*', i):
                    depth += 1
                    i += 2
                elif query.startswith('*/', i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
        elif query[i] == '$' and (match := re.match(r'\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$', query[i:])):
            delimiter = match.group()
            end = query.find(delimiter, i + len(delimiter))
            i = len(query) if end < 0 else end + len(delimiter)
        elif query[i] == '?':
            pieces.append('%s')
            i += 1
            continue
        else:
            i += 1
        pieces.append(query[start:i].replace('%', '%%'))
    return ''.join(pieces)


class Connection:
    def __init__(self, raw, dialect):
        self.raw, self.dialect = raw, dialect

    def execute(self, query, params=None):
        if self.dialect == 'postgres' and params is not None:
            query = postgres_parameters(query)
        return self.raw.execute(query) if params is None else self.raw.execute(query, params)

    def insert_id(self, query, params):
        if self.dialect == 'postgres':
            return self.execute(query.rstrip().rstrip(';') + ' RETURNING id', params).fetchone()['id']
        return self.execute(query, params).lastrowid

    def lock_users(self, user_ids):
        ids = sorted(set(user_ids))
        if self.dialect == 'sqlite':
            self.execute('BEGIN IMMEDIATE')
        elif ids:
            placeholders = ','.join('?' for _ in ids)
            self.execute(f'SELECT id FROM users WHERE id IN ({placeholders}) ORDER BY id FOR NO KEY UPDATE', ids).fetchall()

    def lock_group(self, group_id):
        if self.dialect == "sqlite":
            self.execute("BEGIN IMMEDIATE")
        else:
            self.execute("SELECT id FROM groups WHERE id=? FOR UPDATE", (group_id,)).fetchall()

    def __getattr__(self, name):
        return getattr(self.raw, name)


def is_unique_violation(exc):
    if isinstance(exc, sqlite3.IntegrityError):
        return getattr(exc, 'sqlite_errorcode', None) in (sqlite3.SQLITE_CONSTRAINT_UNIQUE, sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY)
    return getattr(exc, 'sqlstate', None) == '23505'

class Database:
    def __init__(self, target: str | Path, schema: str | None = None):
        self.dialect = "postgres" if str(target).startswith(("postgres://", "postgresql://")) else "sqlite"
        self.schema = schema if schema is not None else os.environ.get("PRAYER_DATABASE_SCHEMA", "public")
        if self.dialect == "postgres":
            self.path = str(target)
            self._migrate_postgres()
            return
        if "://" in str(target):
            raise ValueError("Unsupported database URL")
        path = target
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
            if "can_supervise" not in columns:
                try:
                    conn.execute("ALTER TABLE users ADD COLUMN can_supervise INTEGER NOT NULL DEFAULT 0 CHECK(can_supervise IN (0,1))")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS supervisor_children (
                    supervisor_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    child_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(supervisor_id, child_id),
                    CHECK(supervisor_id != child_id)
                );
                CREATE INDEX IF NOT EXISTS supervisor_child ON supervisor_children(child_id);
                CREATE TABLE IF NOT EXISTS supervisor_invites (
                    id INTEGER PRIMARY KEY,
                    supervisor_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    revoked_at REAL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS supervisor_active_invite
                    ON supervisor_invites(supervisor_id) WHERE revoked_at IS NULL;
                CREATE TABLE IF NOT EXISTS supervisor_invite_acceptances (
                    invite_id INTEGER NOT NULL REFERENCES supervisor_invites(id) ON DELETE CASCADE,
                    child_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    request_key TEXT NOT NULL,
                    accepted_at REAL NOT NULL,
                    PRIMARY KEY(child_id, request_key)
                );
            """)
        self._migrate_display_names_sqlite()
        self._migrate_groups_sqlite()
        # The file contains hashes and private progress: do not make it world-readable.
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _migrate_display_names_sqlite(self):
        with self.connect() as conn:
            conn.lock_users([])
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
            if "display_name" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
                conn.execute("UPDATE users SET display_name=alias")

    def _migrate_groups_sqlite(self):
        from .groups_schema import SQLITE_SCHEMA, migrate_legacy
        with self.connect() as conn:
            conn.lock_group(0)
            for statement in SQLITE_SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            if not conn.execute("SELECT 1 FROM app_migrations WHERE name='groups_v1'").fetchone():
                migrate_legacy(conn)
                conn.execute("INSERT INTO app_migrations(name) VALUES('groups_v1')")
            if not conn.execute("SELECT 1 FROM app_migrations WHERE name='groups_v2'").fetchone():
                columns = {row['name'] for row in conn.execute('PRAGMA table_info(group_invites)')}
                if 'share_token' not in columns:
                    conn.execute('ALTER TABLE group_invites ADD COLUMN share_token TEXT')
                conn.execute('DROP INDEX IF EXISTS group_active_invite')
                conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS group_active_legacy_invite ON group_invites(group_id) WHERE revoked_at IS NULL AND share_token IS NULL')
                conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS group_active_share_invite ON group_invites(group_id) WHERE revoked_at IS NULL AND share_token IS NOT NULL')
                conn.execute("INSERT INTO app_migrations(name) VALUES('groups_v2')")

    def _connect_postgres(self, **kwargs):
        import psycopg
        try:
            return psycopg.connect(self.path, **kwargs)
        except Exception:
            # DSN parsing and connection errors may include URL credentials.
            # Keep SQL errors after connection unchanged for callers' sqlstate checks.
            raise RuntimeError('PostgreSQL connection failed; check database configuration') from None

    def _migrate_postgres(self):
        from psycopg import sql
        migrations = sorted((Path(__file__).parent / 'migrations').glob('[0-9]*.sql'))
        with self._connect_postgres() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(1347567954)")
            conn.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(self.schema)))
            conn.execute(sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(self.schema)))
            conn.execute('CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at DOUBLE PRECISION NOT NULL)')
            applied = {row[0] for row in conn.execute('SELECT version FROM schema_migrations')}
            known = {int(path.name.split('_', 1)[0]) for path in migrations}
            if applied - known:
                raise RuntimeError('Database schema is newer than this application')
            for path in migrations:
                version = int(path.name.split('_', 1)[0])
                if version not in applied:
                    conn.execute(path.read_text())
                    conn.execute('INSERT INTO schema_migrations(version,applied_at) VALUES(%s,%s)', (version, time.time()))

    @contextmanager
    def connect(self):
        if self.dialect == 'postgres':
            from psycopg import sql
            def row_factory(cursor):
                names = [column.name for column in cursor.description] if cursor.description else []
                return lambda values: Row(zip(names, values))
            conn = self._connect_postgres(row_factory=row_factory)
        else:
            conn = sqlite3.connect(self.path, timeout=10)
            conn.row_factory = sqlite3.Row
        try:
            if self.dialect == 'postgres':
                conn.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
                conn.execute(sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(self.schema)))
            else:
                conn.execute('PRAGMA foreign_keys=ON')
            yield Connection(conn, self.dialect)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_day(self, user_id: int, day: str) -> dict:
        from .main import row_day, blank_day
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM days WHERE user_id=? AND date=?", (user_id, day)).fetchone()
            return row_day(row) if row else blank_day(day)

    def update_day(self, user_id: int, day: str, version: int | None, mutate) -> dict:
        from .main import row_day, blank_day, snapshot
        with self.connect() as conn:
            # Serialize read-modify-write so rapid cycles and independent blocks do not overwrite each other.
            conn.lock_users([user_id])
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
        from .main import row_day, snapshot
        sql, params = "SELECT * FROM days WHERE user_id=?", [user_id]
        if start is not None:
            sql += " AND date BETWEEN ? AND ?"
            params += [start, end]
        with self.connect() as conn:
            return [snapshot(row_day(row)) for row in conn.execute(sql + " ORDER BY date", params).fetchall()]


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Initialize or upgrade PostgreSQL storage')
    parser.add_argument('command', choices=['migrate'])
    parser.parse_args()
    target = os.environ.get('PRAYER_DATABASE_URL', '')
    if not target.startswith(('postgresql://', 'postgres://')):
        parser.error('PRAYER_DATABASE_URL must be a PostgreSQL URL')
    try:
        Database(target)
    except Exception:
        # Connection exception messages can contain credentials from a DSN.
        parser.exit(1, 'Database migration failed; check connection and schema compatibility.\n')
    print('Database migrations applied successfully.')


if __name__ == '__main__':
    main()
