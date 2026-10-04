import hashlib
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import psycopg
from psycopg import sql

from backend.import_sqlite import ImportValidationError, import_sqlite, read_snapshot


def fixture(path, *, legacy=False):
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE users(id INTEGER PRIMARY KEY,alias TEXT NOT NULL,
                alias_key TEXT NOT NULL UNIQUE,password_hash TEXT NOT NULL,created_at REAL NOT NULL);
            CREATE TABLE sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),expires_at REAL NOT NULL);
            CREATE TABLE days(user_id INTEGER NOT NULL REFERENCES users(id),date TEXT NOT NULL,
                prayers TEXT NOT NULL,sunnah TEXT NOT NULL,dhuhr_before_blocks TEXT NOT NULL,
                version INTEGER NOT NULL,updated_at REAL NOT NULL,PRIMARY KEY(user_id,date));
        """)
        conn.executemany("INSERT INTO users VALUES(?,?,?,?,?)", [
            (7, "fixture-parent", "fixture-parent", "hash-parent", 123.25),
            (21, "fixture-child", "fixture-child", "hash-child", 125.5)])
        conn.execute("INSERT INTO sessions VALUES(?,?,?)", ("session-hash", 21, 9999999999.25))
        prayers = json.dumps(dict.fromkeys(("fajr", "dhuhr", "asr", "maghrib", "isha"), 2), indent=2)
        sunnah = '{"fajr_before":2,"dhuhr_before":2,"dhuhr_after":0,"maghrib_after":0,"isha_after":0,"witr":true}'
        conn.execute("INSERT INTO days VALUES(?,?,?,?,?,?,?)", (21, "2026-10-04", prayers, sunnah, '[true, false]', 8, 1234.125))
        if not legacy:
            conn.executescript("""
                ALTER TABLE users ADD COLUMN city_id TEXT;
                ALTER TABLE users ADD COLUMN can_supervise INTEGER NOT NULL DEFAULT 0;
                CREATE TABLE supervisor_children(supervisor_id INTEGER NOT NULL REFERENCES users(id),child_id INTEGER NOT NULL REFERENCES users(id),created_at REAL NOT NULL,PRIMARY KEY(supervisor_id,child_id));
                CREATE TABLE supervisor_invites(id INTEGER PRIMARY KEY,supervisor_id INTEGER NOT NULL REFERENCES users(id),token_hash TEXT NOT NULL UNIQUE,created_at REAL NOT NULL,revoked_at REAL);
                CREATE TABLE supervisor_invite_acceptances(invite_id INTEGER NOT NULL REFERENCES supervisor_invites(id),child_id INTEGER NOT NULL REFERENCES users(id),request_key TEXT NOT NULL,accepted_at REAL NOT NULL,PRIMARY KEY(child_id,request_key));
                UPDATE users SET city_id='riyadh',can_supervise=1 WHERE id=7;
                INSERT INTO supervisor_children VALUES(7,21,222.5);
                INSERT INTO supervisor_invites VALUES(40,7,'invite-hash',223.5,NULL);
                INSERT INTO supervisor_invite_acceptances VALUES(40,21,'fixture-request-key',224.5);
            """)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "source.sqlite3"
        fixture(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_read_only_exact_data(self):
        before = hashlib.sha256(self.path.read_bytes()).digest()
        data = read_snapshot(self.path)
        self.assertEqual(data["users"][0], (7, "fixture-parent", "fixture-parent", "hash-parent", 123.25, "riyadh", 1, "fixture-parent"))
        self.assertEqual(data["days"][0][-2:], (8, 1234.125))
        self.assertEqual(data["supervisor_invite_acceptances"][0], (40, 21, "fixture-request-key", 224.5))
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).digest())

    def test_display_name_migration_preserves_edits_and_imports_names(self):
        from backend.database import Database
        database = Database(self.path)
        with database.connect() as conn:
            self.assertEqual(conn.execute('SELECT display_name FROM users WHERE id=7').fetchone()[0], 'fixture-parent')
            conn.execute('UPDATE users SET display_name=? WHERE id=7', ('اسم جديد',))
        Database(self.path)
        self.assertEqual(read_snapshot(self.path)['users'][0][-1], 'اسم جديد')
        with database.connect() as conn:
            conn.execute('UPDATE users SET display_name=? WHERE id=7', ('bad\nname',))
        with self.assertRaises(ImportValidationError):
            read_snapshot(self.path)

    def test_legacy_defaults(self):
        path = Path(self.temp.name) / "legacy.sqlite3"
        fixture(path, legacy=True)
        data = read_snapshot(path)
        self.assertEqual(data["users"][0][-3:], (None, 0, "fixture-parent"))
        self.assertEqual(data["supervisor_invites"], [])

    def test_legacy_groups_and_new_snapshot_do_not_resurrect_memberships(self):
        from backend.database import Database
        old = read_snapshot(self.path)
        self.assertEqual(old["groups"][0][2:4], (7, 0))
        self.assertIn((7,21,222.5), old["group_members"])
        self.assertEqual(old["group_invites"][0], (40,7,"invite-hash",223.5,None,None))
        database = Database(self.path)
        with database.connect() as conn:
            conn.execute("DELETE FROM group_members WHERE user_id=21")
        new = read_snapshot(self.path)
        self.assertFalse(any(row[1] == 21 for row in new["group_members"]))
        self.assertEqual(new["supervisor_children"], [(7,21,222.5)])
        self.assertEqual(new["group_invite_acceptances"], [(40,21,"fixture-request-key",224.5)])

    def test_old_complete_groups_snapshot_remains_authoritative(self):
        from backend.database import Database
        database = Database(self.path)
        with database.connect() as conn:
            conn.execute('DELETE FROM group_members WHERE user_id=21')
            conn.execute('DROP INDEX group_active_share_invite')
            conn.execute('DROP INDEX group_active_legacy_invite')
            conn.execute('ALTER TABLE group_invites DROP COLUMN share_token')
            conn.execute("DELETE FROM app_migrations WHERE name='groups_v2'")
        data = read_snapshot(self.path)
        self.assertFalse(any(row[1] == 21 for row in data['group_members']))
        self.assertEqual(data['group_invites'][0][-1], None)
        upgraded = Database(self.path)
        with upgraded.connect() as conn:
            self.assertIsNotNone(conn.execute("SELECT 1 FROM app_migrations WHERE name='groups_v2'").fetchone())
            self.assertIsNone(conn.execute('SELECT 1 FROM group_members WHERE user_id=21').fetchone())

    def test_durable_snapshot_preserves_and_validates_tokens(self):
        from backend.database import Database
        database = Database(self.path)
        token = 'A' * 43
        hashed = hashlib.sha256(token.encode()).hexdigest()
        with database.connect() as conn:
            conn.execute('INSERT INTO group_invites(group_id,token_hash,created_at,share_token) SELECT id,?,0,? FROM groups WHERE admin_id=7', (hashed, token))
        data = read_snapshot(self.path)
        self.assertIn(token, [row[-1] for row in data['group_invites']])
        with database.connect() as conn:
            conn.execute('UPDATE group_invites SET share_token=? WHERE share_token IS NOT NULL', ('B'*43,))
        with self.assertRaises(ImportValidationError):
            read_snapshot(self.path)

    def test_partial_group_schema_rejected(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE groups(id INTEGER,name TEXT,admin_id INTEGER,members_can_view_records INTEGER,created_at REAL)")
        with self.assertRaises(ImportValidationError):
            read_snapshot(self.path)

    def test_partial_group_schema_without_groups_is_not_legacy(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE group_members(group_id INTEGER,user_id INTEGER,created_at REAL)")
        with self.assertRaises(ImportValidationError):
            read_snapshot(self.path)

    def test_invalid_json_and_broken_foreign_key_rejected(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE days SET prayers='not json'")
        with self.assertRaises(ImportValidationError):
            read_snapshot(self.path)
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM days")
            conn.execute("UPDATE sessions SET user_id=999")
        with self.assertRaises(ImportValidationError):
            read_snapshot(self.path)

    def test_missing_schema_rejected(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("DROP TABLE days")
        with self.assertRaises(ImportValidationError):
            read_snapshot(self.path)


@unittest.skipUnless(os.environ.get("PRAYER_TEST_DATABASE_URL"), "PRAYER_TEST_DATABASE_URL is required")
class PostgresImportTests(unittest.TestCase):
    def setUp(self):
        SnapshotTests.setUp(self)
        from backend.database import Database
        self.url = os.environ["PRAYER_TEST_DATABASE_URL"]
        self.schema = "test_import_" + uuid.uuid4().hex
        self.db = Database(self.url, schema=self.schema)

    def tearDown(self):
        try:
            with psycopg.connect(self.url) as conn:
                conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))
        finally:
            SnapshotTests.tearDown(self)

    def test_import_preserves_all_rows_and_sequences(self):
        data = read_snapshot(self.path)
        before = hashlib.sha256(self.path.read_bytes()).digest()
        counts = import_sqlite(self.path, self.url, schema=self.schema)
        self.assertEqual(counts, {table: len(rows) for table, rows in data.items()})
        with self.db.connect() as conn:
            for table, rows in data.items():
                self.assertEqual(conn.execute(f'SELECT COUNT(*) AS count FROM "{table}"').fetchone()["count"], len(rows))
            user = conn.execute("INSERT INTO users(alias,alias_key,password_hash,created_at,display_name) VALUES(?,?,?,?,?) RETURNING id", ("next-user", "next-user", "hash", 1.0, "next-user")).fetchone()
            self.assertEqual(user["id"], 22)
            invite = conn.execute("INSERT INTO supervisor_invites(supervisor_id,token_hash,created_at,revoked_at) VALUES(?,?,?,?) RETURNING id", (7, "next-invite", 1.0, 2.0)).fetchone()
            self.assertEqual(invite["id"], 41)
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).digest())

    def test_import_preserves_canonical_link_alongside_legacy(self):
        from backend.database import Database
        source = Database(self.path)
        token = 'A' * 43
        hashed = hashlib.sha256(token.encode()).hexdigest()
        with source.connect() as conn:
            conn.execute('DELETE FROM group_members WHERE user_id=21')
            conn.execute('INSERT INTO group_invites(group_id,token_hash,created_at,share_token) SELECT id,?,0,? FROM groups WHERE admin_id=7', (hashed, token))
        import_sqlite(self.path, self.url, schema=self.schema)
        with self.db.connect() as conn:
            self.assertEqual(dict(conn.execute('SELECT token_hash,share_token FROM group_invites WHERE share_token IS NOT NULL').fetchone()), {'token_hash': hashed, 'share_token': token})
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM group_invites WHERE revoked_at IS NULL').fetchone()[0], 2)
            self.assertIsNone(conn.execute('SELECT 1 FROM group_members WHERE user_id=21').fetchone())

    def test_dry_run_rolls_back_and_nonempty_target_is_refused(self):
        import_sqlite(self.path, self.url, schema=self.schema, dry_run=True)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"], 0)
            row = conn.execute("INSERT INTO users(alias,alias_key,password_hash,created_at,display_name) VALUES(?,?,?,?,?) RETURNING id", ("existing-user", "existing-user", "hash", 1.0, "existing-user")).fetchone()
            self.assertEqual(row["id"], 1)
        with self.assertRaises(ImportValidationError):
            import_sqlite(self.path, self.url, schema=self.schema)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) AS count FROM days").fetchone()["count"], 0)

    def test_late_constraint_failure_rolls_back_all_tables(self):
        # Valid SQLite FK but PostgreSQL disallows a duplicate active invite.
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO supervisor_invites VALUES(41,7,'second-active',225.0,NULL)")
        with self.assertRaises(psycopg.IntegrityError):
            import_sqlite(self.path, self.url, schema=self.schema)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) AS count FROM days").fetchone()["count"], 0)

    def test_legacy_import(self):
        path = Path(self.temp.name) / "legacy.sqlite3"
        fixture(path, legacy=True)
        counts = import_sqlite(path, self.url, schema=self.schema)
        self.assertEqual(counts["users"], 2)
        with self.db.connect() as conn:
            row = conn.execute("SELECT city_id,can_supervise FROM users WHERE id=7").fetchone()
            self.assertIsNone(row["city_id"])
            self.assertEqual(row["can_supervise"], 0)

    def test_imported_password_and_session_authenticate(self):
        import httpx
        from backend.main import create_app, password_hash, token_hash
        password = "FixturePassword!42"
        token = "fixture-import-session-token"
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE users SET password_hash=? WHERE id=21", (password_hash(password),))
            conn.execute("UPDATE sessions SET token_hash=?", (token_hash(token),))
        import_sqlite(self.path, self.url, schema=self.schema)
        with patch.dict(os.environ, {"PRAYER_DATABASE_SCHEMA": self.schema}):
            app = create_app(self.url)

        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                me = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + token})
                self.assertEqual(me.status_code, 200, me.text)
                self.assertEqual(me.json()["user"]["id"], 21)
                login = await client.post("/api/v1/auth/login", json={"alias": "fixture-child", "password": password})
                self.assertEqual(login.status_code, 200, login.text)
                self.assertEqual(login.json()["user"]["id"], 21)
        asyncio.run(check())

    def test_configured_schema_overrides_connection_search_path(self):
        from backend.database import Database
        shadow = "test_import_shadow_" + uuid.uuid4().hex
        Database(self.url, schema=shadow)
        try:
            parts = urlsplit(self.url)
            query = [(key, value) for key, value in parse_qsl(parts.query) if key != "options"]
            query.append(("options", "-c search_path=" + shadow))
            url = urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))
            with patch.dict(os.environ, {"PRAYER_DATABASE_SCHEMA": self.schema}):
                import_sqlite(self.path, url)
            with self.db.connect() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"], 2)
            with psycopg.connect(self.url) as conn:
                count = conn.execute(sql.SQL("SELECT COUNT(*) FROM {}.users").format(sql.Identifier(shadow))).fetchone()[0]
                self.assertEqual(count, 0)
        finally:
            with psycopg.connect(self.url) as conn:
                conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(shadow)))
