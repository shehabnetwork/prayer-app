"""Run existing API behavior against real PostgreSQL in isolated disposable schemas.

Opt in with PRAYER_TEST_DATABASE_URL. Every fixture owns randomly named schemas;
no application/public tables are truncated or dropped.
"""
import asyncio
import os
import secrets
import unittest
from unittest.mock import patch

from backend import main
from backend.tests import test_api, test_daily_review, test_prayer_times, test_groups

URL = os.environ.get('PRAYER_TEST_DATABASE_URL')


class PostgresFixture:
    base_module = None

    async def asyncSetUp(self):
        import psycopg
        from psycopg import sql
        self.pg_schemas = {}
        real_factory = main.create_app

        def factory(path=None, **kwargs):
            key = str(path)
            if key not in self.pg_schemas:
                schema = 'test_prayer_' + secrets.token_hex(10)
                with psycopg.connect(URL, autocommit=True) as conn:
                    conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
                self.pg_schemas[key] = schema
            with patch.dict(os.environ, {'PRAYER_DATABASE_SCHEMA': self.pg_schemas[key]}):
                return real_factory(URL, **kwargs)

        self.factory_patch = patch.object(self.base_module, 'create_app', factory)
        self.factory_patch.start()
        self.addCleanup(self.factory_patch.stop)
        self.addCleanup(self.drop_schemas)
        await super().asyncSetUp()

    def drop_schemas(self):
        import psycopg
        from psycopg import sql
        with psycopg.connect(URL, autocommit=True) as conn:
            for schema in self.pg_schemas.values():
                conn.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))


@unittest.skipUnless(URL, 'set PRAYER_TEST_DATABASE_URL for real PostgreSQL validation')
class PostgresAPITests(PostgresFixture, test_api.APITests):
    base_module = test_api

    async def test_schema_initialization_is_repeatable_and_rejects_future_versions(self):
        from concurrent.futures import ThreadPoolExecutor
        from backend.database import Database
        schema = self.app.state.database.schema
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda _: Database(URL, schema=schema), range(4)))
        with self.app.state.database.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM schema_migrations').fetchone()[0], 4)
            conn.execute('INSERT INTO schema_migrations VALUES(?,?)', (999, 0))
        with self.assertRaises(RuntimeError):
            Database(URL, schema=schema)

    async def test_parameter_translation_preserves_literals_comments_and_percent(self):
        with self.app.state.database.connect() as conn:
            row = conn.execute("SELECT '?' AS literal, ? AS value, '10%' AS ratio /* ? */", (4,)).fetchone()
        self.assertEqual(dict(row), {'literal': '?', 'value': 4, 'ratio': '10%'})
        self.assertEqual(row[0], '?')

    async def test_connection_errors_hide_credentials_at_startup_and_runtime(self):
        from backend.database import Database
        malformed = 'postgresql://user:CANARY_SECRET%@localhost/db'
        with self.assertRaises(RuntimeError) as caught:
            Database(malformed)
        self.assertNotIn('CANARY_SECRET', str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        database = self.app.state.database
        original = database.path
        database.path = malformed
        try:
            with self.assertRaises(RuntimeError) as caught:
                with database.connect():
                    pass
            self.assertNotIn('CANARY_SECRET', str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)
        finally:
            database.path = original

    async def test_bad_database_url_does_not_fall_back(self):
        with patch.dict(os.environ, {'PRAYER_DATABASE_URL': 'broken-url'}):
            with patch.object(main, 'Database') as factory:
                with self.assertRaises(ValueError):
                    main.create_app()
                factory.assert_not_called()

    async def test_same_version_concurrent_first_day_writes_conflict(self):
        await self.register()
        responses = await asyncio.gather(*[
            self.client.post(self.day + '/prayers/fajr/cycle', json={'version': 0})
            for _ in range(4)
        ])
        self.assertEqual(sorted(r.status_code for r in responses), [200, 409, 409, 409])
        record = (await self.client.get(self.day)).json()
        self.assertEqual(record['version'], 1)
        self.assertEqual(record['prayers']['fajr'], 1)


@unittest.skipUnless(URL, 'set PRAYER_TEST_DATABASE_URL for real PostgreSQL validation')
class PostgresGroupTests(PostgresFixture, test_groups.GroupTests):
    base_module = test_groups

    async def test_populated_postgres_upgrade_preserves_links_and_runs_once(self):
        from pathlib import Path
        import psycopg
        from psycopg import sql
        from backend.database import Database

        schema = 'test_group_upgrade_' + secrets.token_hex(10)
        self.pg_schemas['legacy-upgrade'] = schema
        with psycopg.connect(URL) as conn:
            conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            conn.execute(sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(schema)))
            conn.execute((Path(main.__file__).parent / 'migrations/001_initial.sql').read_text())
            conn.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at DOUBLE PRECISION NOT NULL)')
            conn.execute('INSERT INTO schema_migrations VALUES(1,0)')
            conn.execute("INSERT INTO users(id,alias,alias_key,password_hash,created_at) VALUES(7,'admin','admin','hash',1),(21,'member','member','hash',2)")
            conn.execute('INSERT INTO supervisor_children VALUES(7,21,3)')
            conn.execute("INSERT INTO supervisor_invites VALUES(40,7,'original-hash',4,NULL)")
            conn.execute("INSERT INTO supervisor_invite_acceptances VALUES(40,21,'original-request-key',5)")

        database = Database(URL, schema=schema)
        with database.connect() as conn:
            self.assertEqual(conn.execute('SELECT display_name FROM users WHERE id=7').fetchone()[0], 'admin')
            conn.execute("UPDATE users SET display_name='Updated name' WHERE id=7")
            self.assertEqual(conn.execute('SELECT admin_id,members_can_view_records FROM groups').fetchone()[0], 7)
            self.assertEqual(conn.execute('SELECT members_can_view_records FROM groups').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM group_members').fetchone()[0], 2)
            self.assertEqual(dict(conn.execute('SELECT id,group_id,token_hash,revoked_at FROM group_invites').fetchone()),
                             {'id': 40, 'group_id': 7, 'token_hash': 'original-hash', 'revoked_at': None})
            self.assertEqual(conn.execute('SELECT request_key FROM group_invite_acceptances').fetchone()[0], 'original-request-key')
            conn.execute('DELETE FROM group_members WHERE user_id=21')
        database = Database(URL, schema=schema)
        with database.connect() as conn:
            self.assertEqual(conn.execute('SELECT display_name FROM users WHERE id=7').fetchone()[0], 'Updated name')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM groups').fetchone()[0], 1)
            self.assertIsNone(conn.execute('SELECT 1 FROM group_members WHERE user_id=21').fetchone())
            next_group = conn.insert_id("INSERT INTO groups(name,admin_id,created_at) VALUES('next',7,6)", ())
            self.assertGreater(next_group, 7)
            next_invite = conn.insert_id("INSERT INTO group_invites(group_id,token_hash,created_at) VALUES(?,'next-hash',6)", (next_group,))
            self.assertGreater(next_invite, 40)

    @unittest.skip('SQLite conversion fixture; group import covers PostgreSQL conversion')
    async def test_legacy_migration_once_and_retired_routes(self):
        pass

    async def test_accept_rechecks_revocation_after_group_lock(self):
        import threading
        from backend.database import Connection
        admin, _ = await self.account('guard-admin')
        member, _ = await self.account('guard-member')
        invite = await self.invite(admin)
        gid = self.group_ids[id(admin)]
        started = threading.Event()
        original = Connection.lock_group
        def waiting(conn, group_id):
            started.set()
            return original(conn, group_id)
        with self.app.state.database.connect() as guard:
            guard.lock_group(gid)
            with patch.object(Connection, 'lock_group', waiting):
                task = asyncio.create_task(member.post('/api/v1/group-invites/accept', json={
                    'token': invite['token'], 'request_key': 'guard-request-key'}))
                self.assertTrue(await asyncio.to_thread(started.wait, 3))
                self.assertFalse(task.done())
                guard.execute('UPDATE group_invites SET revoked_at=1 WHERE id=?', (invite['invite']['id'],))
        result = await asyncio.wait_for(task, 10)
        self.assertEqual(result.status_code, 404, result.text)



@unittest.skipUnless(URL, 'set PRAYER_TEST_DATABASE_URL for real PostgreSQL validation')
class PostgresDailyReviewTests(PostgresFixture, test_daily_review.DailyReviewTests):
    base_module = test_daily_review


@unittest.skipUnless(URL, 'set PRAYER_TEST_DATABASE_URL for real PostgreSQL validation')
class PostgresScheduleTests(PostgresFixture, test_prayer_times.PrayerTimeTests):
    base_module = test_prayer_times
