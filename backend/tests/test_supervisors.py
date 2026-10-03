import asyncio
import secrets
import sqlite3
import tempfile
import unittest
from pathlib import Path

import httpx

from backend.main import create_app, token_hash


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'supervisors.sqlite3'
        self.app = create_app(self.path)
        self.clients = []
        self.password = secrets.token_urlsafe(24)

    async def asyncTearDown(self):
        for client in self.clients:
            await client.aclose()
        self.temp.cleanup()

    def client(self, app=None):
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app or self.app),
                                  base_url='http://testserver', headers={'Origin': 'http://testserver'})
        self.clients.append(client)
        return client

    async def account(self, alias, supervisor=False):
        client = self.client()
        response = await client.post('/api/v1/auth/register', json={
            'alias': alias, 'password': self.password,
            'account_type': 'supervisor' if supervisor else 'child'})
        self.assertEqual(response.status_code, 201, response.text)
        return client, response.json()['user']

    async def invite(self, supervisor):
        response = await supervisor.post('/api/v1/supervisor/invites')
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def accept(self, child, invite, key=None):
        response = await child.post('/api/v1/supervisor-invites/accept', json={
            'token': invite['token'], 'request_key': key or secrets.token_urlsafe(24)})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_shared_link_multiple_children_and_multiple_supervisors(self):
        dad, dad_user = await self.account('test-dad', True)
        teacher, teacher_user = await self.account('test-teacher', True)
        first, first_user = await self.account('test-first')
        second, second_user = await self.account('test-second')
        link = await self.invite(dad)
        preview = await first.post('/api/v1/supervisor-invites/preview', json={'token': link['token']})
        self.assertEqual(preview.json()['supervisor']['id'], dad_user['id'])
        self.assertEqual((await first.get('/api/v1/account/supervisors')).json()['supervisors'], [])
        await asyncio.gather(self.accept(first, link), self.accept(second, link))
        await self.accept(first, await self.invite(teacher))
        parents = (await first.get('/api/v1/account/supervisors')).json()['supervisors']
        self.assertEqual({p['id'] for p in parents}, {dad_user['id'], teacher_user['id']})
        children = (await dad.get('/api/v1/supervisor/children')).json()['children']
        self.assertEqual({c['id'] for c in children}, {first_user['id'], second_user['id']})
        await self.accept(first, link)
        self.assertEqual(len((await dad.get('/api/v1/supervisor/children')).json()['children']), 2)

    async def test_authorized_reads_are_read_only_and_revoked_individually(self):
        dad, dad_user = await self.account('read-dad', True)
        teacher, teacher_user = await self.account('read-teacher', True)
        child, child_user = await self.account('read-child')
        outsider, outsider_user = await self.account('read-outsider')
        await self.accept(child, await self.invite(dad))
        await self.accept(child, await self.invite(teacher))
        date = '2026-09-30'
        await child.patch(f'/api/v1/days/{date}/prayers/fajr', json={'state': 2})
        base = f"/api/v1/supervisor/children/{child_user['id']}"
        self.assertEqual((await dad.get(base + f'/days/{date}')).json()['prayers']['fajr'], 2)
        self.assertEqual(len((await dad.get(base + '/history?from=2026-09-01&to=2026-09-30')).json()['days']), 1)
        self.assertEqual((await dad.get(base + '/statistics')).json()['gold_medals'], 27)
        self.assertEqual((await dad.get(f'/api/v1/days/{date}')).json()['prayers']['fajr'], 0)
        self.assertIn((await dad.patch(base + f'/days/{date}/prayers/fajr', json={'state': 0})).status_code, (404, 405))
        self.assertEqual((await child.get(f'/api/v1/days/{date}')).json()['prayers']['fajr'], 2)
        for suffix in (f'/days/{date}', '/history?from=2026-09-01&to=2026-09-30', '/statistics'):
            response = await dad.get(f"/api/v1/supervisor/children/{outsider_user['id']}" + suffix)
            self.assertIn(response.status_code, (403, 404))
        await child.delete(f"/api/v1/account/supervisors/{dad_user['id']}")
        self.assertIn((await dad.get(base + '/statistics')).status_code, (403, 404))
        self.assertEqual((await teacher.get(base + '/statistics')).status_code, 200)
        await teacher.delete(base)
        self.assertEqual((await child.get('/api/v1/account/supervisors')).json()['supervisors'], [])

    async def test_rotate_revoke_and_old_acceptance_retry(self):
        dad, dad_user = await self.account('rotate-dad', True)
        child, child_user = await self.account('rotate-child')
        another, _ = await self.account('rotate-another')
        link = await self.invite(dad)
        key = secrets.token_urlsafe(24)
        await self.accept(child, link, key)
        await child.delete(f"/api/v1/account/supervisors/{dad_user['id']}")
        await child.post('/api/v1/supervisor-invites/accept', json={'token': link['token'], 'request_key': key})
        self.assertEqual((await child.get('/api/v1/account/supervisors')).json()['supervisors'], [])
        await self.accept(child, link)
        self.assertEqual((await dad.post('/api/v1/supervisor/invites')).status_code, 409)
        rotated = await dad.post('/api/v1/supervisor/invites/rotate')
        self.assertIn(rotated.status_code, (200, 201), rotated.text)
        newer = rotated.json()
        rejected = await another.post('/api/v1/supervisor-invites/accept', json={'token': link['token'], 'request_key': secrets.token_urlsafe(24)})
        self.assertIn(rejected.status_code, (404, 409, 410))
        await self.accept(another, newer)
        self.assertEqual(len((await dad.get('/api/v1/supervisor/children')).json()['children']), 2)
        await dad.delete(f"/api/v1/supervisor/invites/{newer['invite']['id']}")
        self.assertIn((await another.post('/api/v1/supervisor-invites/preview', json={'token': newer['token']})).status_code, (404, 409, 410))
        self.assertEqual((await dad.get(f"/api/v1/supervisor/children/{child_user['id']}/statistics")).status_code, 200)

    async def test_capability_self_link_csrf_and_token_hashing(self):
        child, child_user = await self.account('security-child')
        self.assertFalse(child_user['can_supervise'])
        self.assertEqual((await child.post('/api/v1/supervisor/invites')).status_code, 403)
        enabled = await child.patch('/api/v1/account', json={'can_supervise': True})
        self.assertEqual(enabled.status_code, 200)
        self.assertTrue(enabled.json()['user']['can_supervise'])
        link = await self.invite(child)
        self_link = await child.post('/api/v1/supervisor-invites/accept', json={'token': link['token'], 'request_key': secrets.token_urlsafe(24)})
        self.assertIn(self_link.status_code, (403, 409, 422))
        foreign = await child.post('/api/v1/supervisor/invites/rotate', headers={'Origin': 'https://foreign.example'})
        self.assertEqual(foreign.status_code, 403)
        anonymous = self.client()
        self.assertEqual((await anonymous.post('/api/v1/supervisor-invites/accept', json={'token': link['token'], 'request_key': secrets.token_urlsafe(24)})).status_code, 401)
        with self.app.state.database.connect() as conn:
            stored = conn.execute('SELECT token_hash FROM supervisor_invites').fetchone()['token_hash']
        self.assertEqual(stored, token_hash(link['token']))
        self.assertNotIn(link['token'], (await child.get('/api/v1/supervisor/invites')).text)

    async def test_existing_database_migration_is_repeatable(self):
        old_path = Path(self.temp.name) / 'old.sqlite3'
        with sqlite3.connect(old_path) as conn:
            conn.execute('CREATE TABLE users(id INTEGER PRIMARY KEY, alias TEXT NOT NULL, alias_key TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at REAL NOT NULL)')
            conn.execute("INSERT INTO users VALUES(12,'old-user','old-user','preserved-hash',1)")
        migrated = create_app(old_path)
        restarted = create_app(old_path)
        with restarted.state.database.connect() as conn:
            old = conn.execute('SELECT * FROM users WHERE id=12').fetchone()
            self.assertEqual(old['password_hash'], 'preserved-hash')
            self.assertFalse(old['can_supervise'])
            self.assertIsNone(old['city_id'])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM supervisor_children').fetchone()[0], 0)

    async def test_concurrent_acceptance_is_idempotent_and_create_rotate_is_serialized(self):
        dad, dad_user = await self.account('race-dad', True)
        child, child_user = await self.account('race-child')
        invite = await self.invite(dad)
        key = secrets.token_urlsafe(24)
        responses = await asyncio.gather(*(child.post('/api/v1/supervisor-invites/accept', json={
            'token': invite['token'], 'request_key': key}) for _ in range(8)))
        self.assertEqual([r.status_code for r in responses], [200] * 8)
        with self.app.state.database.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM supervisor_children WHERE supervisor_id=? AND child_id=?',
                                          (dad_user['id'], child_user['id'])).fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM supervisor_invite_acceptances WHERE child_id=?',
                                          (child_user['id'],)).fetchone()[0], 1)

        # Both operations serialize under the invite table's single-active-link invariant.
        created, rotated = await asyncio.gather(
            dad.post('/api/v1/supervisor/invites'),
            dad.post('/api/v1/supervisor/invites/rotate'))
        self.assertIn(created.status_code, (201, 409))
        self.assertEqual(rotated.status_code, 201, rotated.text)
        active = (await dad.get('/api/v1/supervisor/invites')).json()['invites']
        self.assertEqual(sum(item['revoked_at'] is None for item in active), 1)
        # Link rotation changes invitation validity while preserving the established grant.
        self.assertEqual((await dad.get(f"/api/v1/supervisor/children/{child_user['id']}/statistics")).status_code, 200)

        late_child, late_user = await self.account('race-late-child')
        current = await dad.post('/api/v1/supervisor/invites/rotate')
        self.assertEqual(current.status_code, 201, current.text)
        old_token = current.json()['token']
        accepted, rotated_again = await asyncio.gather(
            late_child.post('/api/v1/supervisor-invites/accept', json={
                'token': old_token, 'request_key': secrets.token_urlsafe(24)}),
            dad.post('/api/v1/supervisor/invites/rotate'))
        self.assertIn(accepted.status_code, (200, 404), accepted.text)
        self.assertEqual(rotated_again.status_code, 201, rotated_again.text)
        self.assertEqual((await late_child.post('/api/v1/supervisor-invites/preview',
                                                json={'token': old_token})).status_code, 404)
        listed = (await dad.get('/api/v1/supervisor/children')).json()['children']
        self.assertEqual(late_user['id'] in {entry['id'] for entry in listed}, accepted.status_code == 200)

    async def test_invalid_invite_inputs_and_foreign_ownership(self):
        dad, _ = await self.account('invalid-dad', True)
        other, other_user = await self.account('invalid-other', True)
        child, _ = await self.account('invalid-child')
        link = await self.invite(dad)
        for payload in ({'token': 'x'}, {'token': link['token'], 'request_key': 'short'},
                        {'token': link['token'], 'request_key': secrets.token_urlsafe(20), 'extra': 1}):
            response = await child.post('/api/v1/supervisor-invites/accept', json=payload)
            self.assertEqual(response.status_code, 422, response.text)
        key = secrets.token_urlsafe(24)
        self.assertEqual((await child.post('/api/v1/supervisor-invites/accept', json={
            'token': link['token'], 'request_key': key})).status_code, 200)
        self.assertEqual((await child.post('/api/v1/supervisor-invites/accept', json={
            'token': (await self.invite(other))['token'], 'request_key': key})).status_code, 409)
        self.assertEqual((await other.delete('/api/v1/supervisor/invites/' + str(link['invite']['id']))).status_code, 404)
        self.assertIn((await other.get(f"/api/v1/supervisor/children/{other_user['id']}/statistics")).status_code, (403, 404))

    async def test_invite_rate_limit_budgets_are_60_and_600_per_ten_minutes(self):
        dad, _ = await self.account('limit-dad', True)
        link = await self.invite(dad)  # first write request
        for _ in range(59):
            response = await dad.post('/api/v1/supervisor/invites')
            self.assertEqual(response.status_code, 409)
        limited_write = await dad.post('/api/v1/supervisor/invites/rotate')
        self.assertEqual(limited_write.status_code, 429)
        self.assertEqual(limited_write.headers['Retry-After'], '600')
        for _ in range(600):
            response = await dad.post('/api/v1/supervisor-invites/preview', json={'token': link['token']})
            self.assertEqual(response.status_code, 200)
        limited_lookup = await dad.post('/api/v1/supervisor-invites/preview', json={'token': link['token']})
        self.assertEqual(limited_lookup.status_code, 429)
        self.assertEqual(limited_lookup.headers['Retry-After'], '600')

    async def test_supervisor_schedule_uses_child_city(self):
        day = '2026-09-30'

        class FakeSchedules:
            def today(self, city):
                return day

            def schedule(self, city, requested_day):
                return {'available': True, 'timezone': city['timezone'], 'today': day,
                        'times': {}, 'eligible_prayers': ['fajr'] if city['id'] == 'riyadh' else ['isha'],
                        'error': None}

        app = create_app(Path(self.temp.name) / 'cities.sqlite3', schedule_service=FakeSchedules())
        child_client = self.client(app)
        supervisor_client = self.client(app)
        # Clients use a separate database app, so create accounts there.
        child_response = await child_client.post('/api/v1/auth/register', json={
            'alias': 'city-child', 'password': self.password})
        parent_response = await supervisor_client.post('/api/v1/auth/register', json={
            'alias': 'city-parent', 'password': self.password, 'account_type': 'supervisor'})
        self.assertEqual(child_response.status_code, 201, child_response.text)
        self.assertEqual(parent_response.status_code, 201, parent_response.text)
        child_user = child_response.json()['user']
        await child_client.patch('/api/v1/account', json={'city_id': 'riyadh'})
        # The supervisor has a different city and must not control the child's schedule.
        await supervisor_client.patch('/api/v1/account', json={'city_id': 'alexandria'})
        invite = (await supervisor_client.post('/api/v1/supervisor/invites')).json()
        await child_client.post('/api/v1/supervisor-invites/accept', json={
            'token': invite['token'], 'request_key': secrets.token_urlsafe(24)})
        supervised = await supervisor_client.get(f"/api/v1/supervisor/children/{child_user['id']}/days/{day}")
        self.assertEqual(supervised.status_code, 200, supervised.text)
        self.assertEqual(supervised.json()['schedule']['timezone'], 'Asia/Riyadh')
        self.assertEqual(supervised.json()['schedule']['eligible_prayers'], ['fajr'])

    async def test_migration_preserves_existing_session_and_day_rows(self):
        child, _ = await self.account('migration-child')
        session_token = child.cookies.get('prayer_session')
        date = '2026-09-30'
        changed = await child.patch(f'/api/v1/days/{date}/prayers/fajr', json={'state': 2})
        self.assertEqual(changed.status_code, 200, changed.text)
        # Recreate the pre-supervisor schema while keeping its user, session, and day rows.
        with self.app.state.database.connect() as conn:
            conn.execute('DROP TABLE supervisor_invite_acceptances')
            conn.execute('DROP TABLE supervisor_children')
            conn.execute('DROP TABLE supervisor_invites')
            conn.execute('ALTER TABLE users DROP COLUMN can_supervise')
            conn.execute('ALTER TABLE users DROP COLUMN city_id')
        restarted = create_app(self.path)
        migrated_client = self.client(restarted)
        with self.app.state.database.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM supervisor_children').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM supervisor_invites').fetchone()[0], 0)
        restored = await migrated_client.get(f'/api/v1/days/{date}', headers={
            'Authorization': f"Bearer {session_token}"})
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json()['prayers']['fajr'], 2)
        self.assertEqual(restored.json()['version'], 1)
