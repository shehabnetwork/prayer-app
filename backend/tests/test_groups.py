"""Group membership, privacy, migration and legacy retirement integration tests."""
import asyncio
import secrets
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
import httpx
from backend.main import create_app, token_hash


class GroupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'groups.sqlite3'
        self.app = create_app(self.path)
        self.clients = []
        self.password = secrets.token_urlsafe(24)
        self.group_ids = {}

    async def asyncTearDown(self):
        for client in self.clients:
            await client.aclose()
        self.temp.cleanup()

    def client(self, app=None):
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app or self.app), base_url='http://testserver', headers={'Origin': 'http://testserver'})
        self.clients.append(client)
        return client

    async def account(self, alias, supervisor=False):
        client = self.client()
        response = await client.post('/api/v1/auth/register', json={'alias': alias, 'password': self.password})
        self.assertEqual(response.status_code, 201, response.text)
        return client, response.json()['user']

    async def group(self, client):
        response = await client.post('/api/v1/groups', json={'name': 'مجموعة اختبار'})
        self.assertEqual(response.status_code, 201, response.text)
        gid = response.json()['group']['id']
        self.group_ids[id(client)] = gid
        return gid

    async def invite(self, client):
        gid = self.group_ids.get(id(client)) or await self.group(client)
        response = await client.post(f'/api/v1/groups/{gid}/invite-link')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def accept(self, client, invite, key=None):
        response = await client.post('/api/v1/group-invites/accept', json={'token': invite['token'], 'request_key': key or secrets.token_urlsafe(24)})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_display_names_propagate_to_roster_report_and_invitation(self):
        admin, a = await self.account('display-admin')
        member, m = await self.account('display-member')
        invite = await self.invite(admin)
        gid = self.group_ids[id(admin)]
        await self.accept(member, invite)
        await admin.patch('/api/v1/account', json={'display_name': 'اسم المشرف'})
        await member.patch('/api/v1/account', json={'display_name': 'اسم العضو'})
        preview = (await member.post('/api/v1/group-invites/preview', json={'token': invite['token']})).json()
        self.assertEqual(preview['group']['admin_display_name'], 'اسم المشرف')
        self.assertEqual(preview['group']['admin_alias'], 'display-admin')
        roster = (await admin.get(f'/api/v1/groups/{gid}')).json()['members']
        self.assertEqual({u['id']: u['display_name'] for u in roster}, {a['id']: 'اسم المشرف', m['id']: 'اسم العضو'})
        report = (await admin.get(f'/api/v1/groups/{gid}/daily-review?date=2026-09-30')).json()
        self.assertEqual({r['member']['id']: r['member']['display_name'] for r in report['rows']},
                         {a['id']: 'اسم المشرف', m['id']: 'اسم العضو'})

    async def test_group_creation_rolls_back_if_invite_creation_fails(self):
        from backend.database import Connection
        admin, _ = await self.account('atomic-admin')
        original = Connection.insert_id
        def insert(conn, query, params):
            if query.startswith('INSERT INTO group_invites'):
                raise RuntimeError('test invite insert failure')
            return original(conn, query, params)
        with patch.object(Connection, 'insert_id', insert):
            with self.assertRaises(Exception):
                await admin.post('/api/v1/groups', json={'name': 'must roll back'})
        with self.app.state.database.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM groups').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM group_members').fetchone()[0], 0)

    async def test_admin_delete_invalidates_invites_and_preserves_personal_data(self):
        admin, a = await self.account('delete-admin')
        member, m = await self.account('delete-member')
        outsider, _ = await self.account('delete-outsider')
        invite = await self.invite(admin)
        gid = self.group_ids[id(admin)]
        key = secrets.token_urlsafe(24)
        await self.accept(member, invite, key)
        remaining = await self.group(admin)
        for client in (admin, member):
            response = await client.patch('/api/v1/days/2026-09-30/prayers/fajr', json={'state': 2})
            self.assertEqual(response.status_code, 200, response.text)
        path = f'/api/v1/groups/{gid}'
        self.assertEqual((await self.client().delete(path)).status_code, 401)
        for client in (member, outsider):
            self.assertEqual((await client.delete(path)).status_code, 403)
        self.assertEqual((await admin.delete(path, headers={'Origin': 'https://foreign.example'})).status_code, 403)
        with self.app.state.database.connect() as conn:
            personal = {table: [dict(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY user_id' if table == 'days' else f'SELECT * FROM {table} ORDER BY id')]
                        for table in ('users', 'days')}
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM group_invite_acceptances').fetchone()[0], 1)
        response = await admin.delete(path)
        self.assertEqual(response.status_code, 200, response.text)
        with self.app.state.database.connect() as conn:
            for table in ('groups', 'group_members', 'group_invites'):
                column = 'id' if table == 'groups' else 'group_id'
                self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {table} WHERE {column}=?', (gid,)).fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM group_invite_acceptances').fetchone()[0], 0)
            for table in ('users', 'days'):
                rows = [dict(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY user_id' if table == 'days' else f'SELECT * FROM {table} ORDER BY id')]
                self.assertEqual(rows, personal[table])
        self.assertEqual([g['id'] for g in (await admin.get('/api/v1/groups')).json()['groups']], [remaining])
        self.assertEqual((await member.get('/api/v1/groups')).json()['groups'], [])
        self.assertEqual((await outsider.post('/api/v1/group-invites/preview', json={'token': invite['token']})).status_code, 404)
        for request_key in (key, secrets.token_urlsafe(24)):
            self.assertEqual((await member.post('/api/v1/group-invites/accept', json={'token': invite['token'], 'request_key': request_key})).status_code, 404)
        for suffix in (f'/members/{m["id"]}/days/2026-09-30', f'/members/{m["id"]}/history', f'/members/{m["id"]}/statistics', '/daily-review?date=2026-09-30'):
            self.assertEqual((await admin.get(path + suffix)).status_code, 403)
        for client in (admin, member):
            self.assertEqual((await client.get('/api/v1/days/2026-09-30')).json()['prayers']['fajr'], 2)
        # Reinitialization must not recreate a deleted group from inert legacy data.
        restarted = self.client(create_app(self.path))
        await restarted.post('/api/v1/auth/login', json={'alias': a['alias'], 'password': self.password})
        self.assertEqual([g['id'] for g in (await restarted.get('/api/v1/groups')).json()['groups']], [remaining])

    async def test_delete_group_rolls_back_all_dependencies_on_failure(self):
        from backend.database import Connection
        admin, _ = await self.account('rollback-delete-admin')
        member, _ = await self.account('rollback-delete-member')
        invite = await self.invite(admin)
        gid = self.group_ids[id(admin)]
        await self.accept(member, invite)
        original = Connection.execute
        def execute(conn, query, params=()):
            if query == 'DELETE FROM groups WHERE id=?':
                raise RuntimeError('test group delete failure')
            return original(conn, query, params)
        with patch.object(Connection, 'execute', execute):
            with self.assertRaises(Exception):
                await admin.delete(f'/api/v1/groups/{gid}')
        self.assertEqual((await admin.get(f'/api/v1/groups/{gid}')).json()['group']['member_count'], 2)
        self.assertEqual((await member.post('/api/v1/group-invites/preview', json={'token': invite['token']})).status_code, 200)
        with self.app.state.database.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM group_invite_acceptances').fetchone()[0], 1)

    async def test_invite_preview_handles_deletion_between_lookup_and_group_read(self):
        from backend.database import Connection
        admin, _ = await self.account('preview-delete-admin')
        invite = await self.invite(admin)
        gid = self.group_ids[id(admin)]
        original = Connection.execute
        deleted = False
        def execute(conn, query, params=()):
            nonlocal deleted
            if query.startswith('SELECT groups.*,users.alias AS admin_alias') and not deleted:
                deleted = True
                with self.app.state.database.connect() as deleting:
                    deleting.lock_group(gid)
                    original(deleting, 'DELETE FROM group_invites WHERE group_id=?', (gid,))
                    original(deleting, 'DELETE FROM group_members WHERE group_id=?', (gid,))
                    original(deleting, 'DELETE FROM groups WHERE id=?', (gid,))
            return original(conn, query, params)
        with patch.object(Connection, 'execute', execute):
            response = await self.client().post('/api/v1/group-invites/preview', json={'token': invite['token']})
        self.assertTrue(deleted)
        self.assertEqual(response.status_code, 404, response.text)

    async def test_durable_link_is_atomic_private_stable_and_preserves_legacy(self):
        admin, a = await self.account('durable-admin')
        member, _ = await self.account('durable-member')
        gid = await self.group(admin)
        with self.app.state.database.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM group_invites WHERE group_id=? AND share_token IS NOT NULL', (gid,)).fetchone()[0], 1)
            conn.execute('DELETE FROM group_invites WHERE group_id=?', (gid,))
            legacy = secrets.token_urlsafe(32)
            conn.execute('INSERT INTO group_invites(group_id,token_hash,created_at) VALUES(?,?,0)', (gid, token_hash(legacy)))
        responses = await asyncio.gather(*(admin.post(f'/api/v1/groups/{gid}/invite-link') for _ in range(8)))
        self.assertTrue(all(r.status_code == 200 for r in responses))
        tokens = {r.json()['token'] for r in responses}
        self.assertEqual(len(tokens), 1)
        token = tokens.pop()
        self.assertNotEqual(token, legacy)
        self.assertEqual((await admin.post(f'/api/v1/groups/{gid}/invite-link', headers={'Origin':'https://foreign.example'})).status_code, 403)
        await self.accept(member, {'token': legacy})
        self.assertEqual((await member.post(f'/api/v1/groups/{gid}/invite-link')).status_code, 403)
        for path in ('/groups', f'/groups/{gid}', f'/groups/{gid}/invites'):
            self.assertNotIn(token, (await admin.get('/api/v1' + path)).text)
        if self.app.state.database.dialect == 'sqlite':
            restarted = create_app(self.path)
            client = self.client(restarted)
            await client.post('/api/v1/auth/login', json={'alias': a['alias'], 'password': self.password})
            self.assertEqual((await client.post(f'/api/v1/groups/{gid}/invite-link')).json()['token'], token)
            self.assertEqual((await client.post('/api/v1/group-invites/preview', json={'token': legacy})).status_code, 200)
        with self.app.state.database.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM group_invites WHERE group_id=? AND revoked_at IS NULL', (gid,)).fetchone()[0], 2)

    async def test_multiple_groups_and_default_private_owner_writes(self):
        admin, a = await self.account('group-admin')
        member, m = await self.account('group-member')
        outsider, o = await self.account('group-outsider')
        invite = await self.invite(admin)
        gid = self.group_ids[id(admin)]
        await self.accept(member, invite)
        self.assertFalse((await admin.get(f'/api/v1/groups/{gid}')).json()['group']['members_can_view_records'])
        self.assertEqual((await admin.get(f'/api/v1/groups/{gid}')).json()['group']['member_count'], 2)
        base = f'/api/v1/groups/{gid}/members/{m["id"]}'
        await member.patch('/api/v1/days/2026-09-30/prayers/fajr', json={'state': 2})
        self.assertEqual((await member.get(base + '/days/2026-09-30')).json()['prayers']['fajr'], 2)
        self.assertEqual((await admin.get(base + '/days/2026-09-30')).json()['prayers']['fajr'], 2)
        self.assertEqual((await member.get(f'/api/v1/groups/{gid}/members/{a["id"]}/statistics')).status_code, 403)
        self.assertEqual((await outsider.get(f'/api/v1/groups/{gid}')).status_code, 403)
        self.assertIn((await admin.patch(base + '/days/2026-09-30/prayers/fajr', json={'state': 0})).status_code, (404,405))
        await self.invite(outsider)
        self.assertEqual(len((await outsider.get('/api/v1/groups')).json()['groups']), 1)
        gid2 = await self.group(admin)
        self.assertNotEqual(gid, gid2)
        self.assertEqual(len((await admin.get('/api/v1/groups')).json()['groups']), 2)
        for path in (f'/api/v1/groups/{gid}/membership', f'/api/v1/groups/{gid}/members/{a["id"]}'):
            self.assertEqual((await admin.delete(path)).status_code, 409)

    async def test_visibility_toggle_leave_remove_and_replay(self):
        admin, a = await self.account('toggle-admin')
        member, m = await self.account('toggle-member')
        other, o = await self.account('toggle-other')
        invite = await self.invite(admin)
        gid = self.group_ids[id(admin)]
        key = secrets.token_urlsafe(24)
        await self.accept(member, invite, key)
        await self.accept(other, invite)
        paths = [f'/api/v1/groups/{gid}/members/{a["id"]}' + suffix for suffix in ('/days/2026-09-30', '/history?from=2026-09-01&to=2026-09-30', '/statistics')]
        paths.append(f'/api/v1/groups/{gid}/daily-review?date=2026-09-30')
        await admin.patch(f'/api/v1/groups/{gid}', json={'members_can_view_records': True})
        for path in paths:
            self.assertEqual((await member.get(path)).status_code, 200)
        await admin.patch(f'/api/v1/groups/{gid}', json={'members_can_view_records': False})
        for path in paths:
            self.assertEqual((await member.get(path)).status_code, 403)
        await member.delete(f'/api/v1/groups/{gid}/membership')
        retry = await member.post('/api/v1/group-invites/accept', json={'token': invite['token'], 'request_key': key})
        self.assertEqual(retry.status_code, 409)
        self.assertEqual((await member.get('/api/v1/groups')).json()['groups'], [])
        await self.accept(member, invite)
        await admin.delete(f'/api/v1/groups/{gid}/members/{m["id"]}')
        self.assertEqual((await admin.get(f'/api/v1/groups/{gid}/members/{m["id"]}/statistics')).status_code, 403)
        self.assertEqual((await other.patch(f'/api/v1/groups/{gid}', json={'name': 'hijack'})).status_code, 403)

    async def test_invite_privacy_rotation_validation_and_csrf(self):
        admin, _ = await self.account('invite-admin')
        member, _ = await self.account('invite-member')
        invite = await self.invite(admin)
        gid = self.group_ids[id(admin)]
        preview = await member.post('/api/v1/group-invites/preview', json={'token': invite['token']})
        self.assertEqual(set(preview.json()['group']), {'id','name','admin_id','admin_alias','admin_display_name','members_can_view_records'})
        with self.app.state.database.connect() as conn:
            self.assertEqual(conn.execute('SELECT token_hash FROM group_invites').fetchone()[0], token_hash(invite['token']))
        self.assertNotIn(invite['token'], (await admin.get(f'/api/v1/groups/{gid}/invites')).text)
        self.assertEqual((await admin.post(f'/api/v1/groups/{gid}/invites/rotate', headers={'Origin':'https://foreign.example'})).status_code, 403)
        key = secrets.token_urlsafe(24)
        await self.accept(member, invite, key)
        newer = (await admin.post(f'/api/v1/groups/{gid}/invites/rotate')).json()
        self.assertEqual((await member.post('/api/v1/group-invites/preview', json={'token':invite['token']})).status_code, 404)
        self.assertEqual((await member.post('/api/v1/group-invites/accept', json={'token':newer['token'],'request_key':key})).status_code, 409)
        await self.accept(member, invite, key)  # Existing receipt survives link rotation.
        for payload in ({'name':' '}, {'name':'ok','admin_id':999}, {'name':'ok','members_can_view_records':1}):
            self.assertEqual((await admin.post('/api/v1/groups', json=payload)).status_code, 422)
        for payload in ({}, {'name':None}, {'members_can_view_records':None}):
            self.assertEqual((await admin.patch(f'/api/v1/groups/{gid}',json=payload)).status_code, 422)

    async def test_concurrent_acceptance_is_single_receipt(self):
        admin, _ = await self.account('race-admin')
        member, _ = await self.account('race-member')
        invite = await self.invite(admin)
        await asyncio.gather(*(self.accept(member, invite, 'same-request-key-for-retry') for _ in range(8)))
        with self.app.state.database.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM group_invite_acceptances').fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM group_members').fetchone()[0],2)

    async def test_schedule_work_rechecks_removal_and_visibility_for_every_read(self):
        admin, a = await self.account('schedule-admin')
        member, m = await self.account('schedule-member')
        invite = await self.invite(admin)
        gid = self.group_ids[id(admin)]
        await self.accept(member, invite)
        await member.patch('/api/v1/account', json={'city_id': 'riyadh'})
        await member.patch('/api/v1/days/2026-09-30/prayers/fajr', json={'state': 1})
        class Schedule:
            callback = None
            def today(self, city):
                return '2026-09-30'
            def schedule(inner, city, day):
                if inner.callback:
                    inner.callback()
                return {'available':True, 'timezone':city['timezone'], 'today':day,
                        'times':{}, 'eligible_prayers':['fajr'], 'error':None}
        schedules = Schedule()
        app = create_app(self.path, schedule_service=schedules)
        reader = self.client(app)
        # Reuse authenticated session without triggering city schedule during auth.
        reader.cookies.update(admin.cookies)
        paths = [f'/api/v1/groups/{gid}/members/{m["id"]}' + suffix for suffix in
                 ('/days/2026-09-30','/history?from=2026-09-30&to=2026-09-30','/statistics')]
        for path in paths:
            with app.state.database.connect() as conn:
                conn.execute('INSERT INTO group_members VALUES(?,?,0) ON CONFLICT(group_id,user_id) DO NOTHING',(gid,m['id']))
            def remove():
                with app.state.database.connect() as conn:
                    conn.execute('DELETE FROM group_members WHERE group_id=? AND user_id=?',(gid,m['id']))
            schedules.callback = remove
            self.assertEqual((await reader.get(path)).status_code,403)
        with app.state.database.connect() as conn:
            conn.execute('INSERT INTO group_members VALUES(?,?,0)',(gid,m['id']))
            conn.execute('UPDATE groups SET members_can_view_records=1 WHERE id=?',(gid,))
        # Shared member reads admin records; admin's selected city drives the schedule.
        await admin.patch('/api/v1/account', json={'city_id':'riyadh'})
        reader.cookies.clear()
        reader.cookies.update(member.cookies)
        def private():
            with app.state.database.connect() as conn:
                conn.execute('UPDATE groups SET members_can_view_records=0 WHERE id=?',(gid,))
        schedules.callback = private
        for suffix in ('/days/2026-09-30','/history?from=2026-09-30&to=2026-09-30','/statistics'):
            with app.state.database.connect() as conn:
                conn.execute('UPDATE groups SET members_can_view_records=1 WHERE id=?',(gid,))
                if suffix.startswith('/history') or suffix == '/statistics':
                    # Ensure a stored day causes schedule evaluation in aggregate APIs.
                    from backend.main import blank_day
                    import json
                    day = blank_day('2026-09-30')
                    conn.execute('INSERT INTO days VALUES(?,?,?,?,?,?,0) ON CONFLICT(user_id,date) DO NOTHING',
                        (a['id'],'2026-09-30',json.dumps(day['prayers']),json.dumps(day['sunnah']),json.dumps(day['dhuhr_before_blocks']),0))
            self.assertEqual((await reader.get(f'/api/v1/groups/{gid}/members/{a["id"]}' + suffix)).status_code,403)

    async def test_invite_rate_limit_budgets(self):
        admin, _ = await self.account('limits-admin')
        invite = await self.invite(admin)
        gid = self.group_ids[id(admin)]
        for _ in range(60):
            self.assertEqual((await admin.post(f'/api/v1/groups/{gid}/invites')).status_code,409)
        self.assertEqual((await admin.post(f'/api/v1/groups/{gid}/invites/rotate')).status_code,429)
        for _ in range(600):
            self.assertEqual((await admin.post('/api/v1/group-invites/preview',json={'token':invite['token']})).status_code,200)
        self.assertEqual((await admin.post('/api/v1/group-invites/preview',json={'token':invite['token']})).status_code,429)

    async def test_legacy_migration_once_and_retired_routes(self):
        admin, a = await self.account('legacy-admin')
        member, m = await self.account('legacy-member')
        token = secrets.token_urlsafe(32)
        with self.app.state.database.connect() as conn:
            conn.execute('DELETE FROM app_migrations')
            conn.execute('INSERT INTO supervisor_children VALUES(?,?,?)', (a['id'],m['id'],0))
            conn.execute('INSERT INTO supervisor_invites VALUES(17,?,?,0,NULL)', (a['id'],token_hash(token)))
            conn.execute('INSERT INTO supervisor_invite_acceptances VALUES(17,?,?,0)', (m['id'],'legacy-request-key'))
        migrated = create_app(self.path)
        with migrated.state.database.connect() as conn:
            group = conn.execute('SELECT * FROM groups').fetchone()
            self.assertFalse(group['members_can_view_records'])
            self.assertEqual(conn.execute('SELECT id FROM group_invites').fetchone()[0],17)
            conn.execute('DELETE FROM group_members WHERE user_id=?',(m['id'],))
        restarted = create_app(self.path)
        with restarted.state.database.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM groups').fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM group_members').fetchone()[0],1)
        client = self.client(restarted)
        response = await client.post('/api/v1/group-invites/preview',json={'token':token})
        self.assertEqual(response.status_code,200)
        for method,path in [('get','/api/v1/supervisor/children'),('post','/api/v1/supervisor-invites/accept'),('get','/api/v1/account/supervisors'),('get',f'/api/v1/supervisor/children/{m["id"]}/statistics')]:
            self.assertEqual((await getattr(client,method)(path)).status_code,410)
