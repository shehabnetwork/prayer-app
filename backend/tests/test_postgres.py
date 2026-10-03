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
from backend.tests import test_api, test_daily_review, test_prayer_times, test_supervisors

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
            self.assertEqual(conn.execute('SELECT count(*) FROM schema_migrations').fetchone()[0], 1)
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
class PostgresSupervisorTests(PostgresFixture, test_supervisors.SupervisorTests):
    base_module = test_supervisors

    @unittest.skip('SQLite legacy fixture; covered by SQLite suite and PostgreSQL importer tests')
    async def test_existing_database_migration_is_repeatable(self):
        pass

    @unittest.skip('SQLite legacy fixture; PostgreSQL data preservation covered by importer tests')
    async def test_migration_preserves_existing_session_and_day_rows(self):
        pass

    async def blocked_acceptance(self, parent_id, child_id, child, invite, key, mutation):
        import threading
        from backend.database import Connection
        started = threading.Event()
        original = Connection.lock_users
        def waiting_lock(conn, ids):
            started.set()
            return original(conn, ids)
        with self.app.state.database.connect() as guard:
            guard.lock_users([parent_id, child_id])
            with patch.object(Connection, 'lock_users', waiting_lock):
                task = asyncio.create_task(child.post('/api/v1/supervisor-invites/accept', json={
                    'token': invite['token'], 'request_key': key}))
                self.assertTrue(await asyncio.to_thread(started.wait, 3))
                self.assertFalse(task.done())
                mutation(guard)
        return await asyncio.wait_for(task, 10)

    async def test_accept_rechecks_revocation_after_waiting_for_guard(self):
        parent, parent_user = await self.account('guard-parent', supervisor=True)
        child, child_user = await self.account('guard-child')
        invite = await self.invite(parent)
        response = await self.blocked_acceptance(parent_user['id'], child_user['id'], child, invite,
            'guard-new-acceptance-key', lambda conn: conn.execute(
                'UPDATE supervisor_invites SET revoked_at=? WHERE id=?', (1, invite['invite']['id'])))
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual((await child.get('/api/v1/account/supervisors')).json()['supervisors'], [])

    async def test_old_retry_cannot_restore_grant_removed_while_waiting(self):
        parent, parent_user = await self.account('unlink-parent', supervisor=True)
        child, child_user = await self.account('unlink-child')
        invite = await self.invite(parent)
        key = 'unlink-old-acceptance-key'
        await self.accept(child, invite, key)
        response = await self.blocked_acceptance(parent_user['id'], child_user['id'], child, invite,
            key, lambda conn: conn.execute('DELETE FROM supervisor_children WHERE supervisor_id=? AND child_id=?',
                                          (parent_user['id'], child_user['id'])))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await child.get('/api/v1/account/supervisors')).json()['supervisors'], [])

    async def test_reciprocal_acceptance_does_not_deadlock(self):
        a, a_user = await self.account('reciprocal-a', supervisor=True)
        b, b_user = await self.account('reciprocal-b', supervisor=True)
        a_invite, b_invite = await asyncio.gather(self.invite(a), self.invite(b))
        await asyncio.wait_for(asyncio.gather(self.accept(a, b_invite), self.accept(b, a_invite)), 10)
        self.assertEqual((await a.get('/api/v1/supervisor/children')).json()['children'][0]['id'], b_user['id'])
        self.assertEqual((await b.get('/api/v1/supervisor/children')).json()['children'][0]['id'], a_user['id'])

    async def test_identical_acceptance_key_is_a_single_receipt(self):
        parent, parent_user = await self.account('receipt-parent', supervisor=True)
        child, child_user = await self.account('receipt-child')
        invite = await self.invite(parent)
        await asyncio.wait_for(asyncio.gather(*[
            self.accept(child, invite, key='same-request-key-for-retry') for _ in range(8)
        ]), 10)
        with self.app.state.database.connect() as conn:
            count = conn.execute('SELECT count(*) FROM supervisor_invite_acceptances').fetchone()[0]
        self.assertEqual(count, 1)
        await child.delete(f"/api/v1/account/supervisors/{parent_user['id']}")
        await self.accept(child, invite, key='same-request-key-for-retry')
        self.assertEqual((await child.get('/api/v1/account/supervisors')).json()['supervisors'], [])


@unittest.skipUnless(URL, 'set PRAYER_TEST_DATABASE_URL for real PostgreSQL validation')
class PostgresDailyReviewTests(PostgresFixture, test_daily_review.DailyReviewTests):
    base_module = test_daily_review


@unittest.skipUnless(URL, 'set PRAYER_TEST_DATABASE_URL for real PostgreSQL validation')
class PostgresScheduleTests(PostgresFixture, test_prayer_times.PrayerTimeTests):
    base_module = test_prayer_times
