import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from backend.main import COOKIE_NAME, blank_day, create_app, token_hash


class ReportSchedules:
    def __init__(self):
        self.calls = []
        self.callback = None
        self.unavailable = set()

    def today(self, city):
        return '2026-10-03'

    def schedule(self, city, day):
        self.calls.append((city['id'], day))
        if self.callback:
            self.callback()
        return {'available': city['id'] not in self.unavailable,
                'timezone': city['timezone'], 'today': day,
                'times': {key: f'{day}T{hour}:00:00+03:00' for key, hour in
                          [('fajr', '05'), ('dhuhr', '12'), ('asr', '15'), ('maghrib', '18'), ('isha', '19')]},
                'eligible_prayers': [], 'source': 'test', 'error': 'provider unavailable'}


class DailyReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.schedules = ReportSchedules()
        self.app = create_app(Path(self.temp.name) / 'daily.sqlite3', schedule_service=self.schedules)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://testserver')
        self.clock = patch('backend.main.datetime', wraps=datetime)
        self.mock_clock = self.clock.start()
        self.mock_clock.now.return_value = datetime(2026, 10, 3, 10, tzinfo=timezone.utc)
        self.add_user(1, 'teacher', supervisor=True)
        with self.app.state.database.connect() as conn:
            conn.execute('INSERT INTO sessions VALUES(?,?,?)', (token_hash('test-token'), 1, time.time() + 3600))
        self.client.cookies.set(COOKIE_NAME, 'test-token')

    async def asyncTearDown(self):
        self.clock.stop()
        await self.client.aclose()
        self.temp.cleanup()

    def add_user(self, user_id, alias, city=None, supervisor=False, linked=True):
        with self.app.state.database.connect() as conn:
            conn.execute('INSERT INTO users(id,alias,alias_key,password_hash,created_at,city_id,can_supervise) VALUES(?,?,?,?,?,?,?)',
                         (user_id, alias, alias.casefold(), 'unused', 0, city, int(supervisor)))
            if linked and user_id != 1:
                conn.execute('INSERT INTO supervisor_children VALUES(?,?,?)', (1, user_id, 0))

    def save(self, user_id, prayers=None, blocks=None, witr=False, date='2026-10-02'):
        day = blank_day(date)
        day['prayers'].update(prayers or {})
        day['dhuhr_before_blocks'] = blocks or [False, False]
        day['sunnah']['dhuhr_before'] = sum(day['dhuhr_before_blocks']) * 2
        day['sunnah']['witr'] = witr
        with self.app.state.database.connect() as conn:
            conn.execute('INSERT INTO days VALUES(?,?,?,?,?,?,?)', (user_id, date, json.dumps(day['prayers']),
                         json.dumps(day['sunnah']), json.dumps(day['dhuhr_before_blocks']), 1, 0))

    async def report(self, query='date=2026-10-02'):
        return await self.client.get('/api/v1/supervisor/daily-review?' + query)

    async def test_completion_sort_precedes_medals_and_pagination_preserves_controls(self):
        for user_id, alias in [(2, 'Zulu'), (3, 'Alpha'), (4, 'Bravo'), (5, 'Empty'), (6, 'Zero')]:
            self.add_user(user_id, alias, 'jeddah')
        self.add_user(7, 'Outsider', linked=False)
        self.save(2, {'fajr': 1, 'dhuhr': 1}, [False, True], True)
        self.save(3, {'fajr': 2})
        self.save(4, {'fajr': 1, 'dhuhr': 1})
        self.save(6)
        pages = [(await self.report(f'date=2026-10-02&limit=2&offset={offset}')).json() for offset in (0, 2, 4)]
        rows = [row for page in pages for row in page['rows']]
        self.assertEqual([row['student']['id'] for row in rows], [2, 4, 3, 5, 6])
        self.assertTrue(all(page['total_students'] == 5 for page in pages))
        self.assertEqual(rows[0]['day']['dhuhr_before_blocks'], [False, True])
        self.assertTrue(rows[0]['day']['sunnah']['witr'])
        self.assertEqual(rows[0]['day']['stats']['silver_medals'], 2)
        self.assertFalse(rows[3]['has_record'])
        self.assertTrue(rows[4]['has_record'])
        self.assertEqual(self.schedules.calls, [])
        self.assertEqual((await self.report()).headers['cache-control'], 'no-store')
        with self.app.state.database.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM days').fetchone()[0], 4)

    async def test_current_schedule_grouping_unknown_last_and_future_eligibility(self):
        for user_id, city in [(2, 'jeddah'), (3, 'jeddah'), (4, 'makkah'), (5, None)]:
            self.add_user(user_id, f'child-{user_id}', city)
            self.save(user_id, {'fajr': 1, 'asr': 2}, [False, True], True, date='2026-10-03')
        self.schedules.unavailable.add('makkah')
        data = (await self.report('date=2026-10-03')).json()
        self.assertEqual([row['student']['id'] for row in data['rows']], [5, 2, 3, 4])
        self.assertEqual(self.schedules.calls, [('jeddah', '2026-10-03'), ('makkah', '2026-10-03')])
        known = data['rows'][1]['day']
        self.assertEqual(known['stats']['completed'], 1)
        self.assertEqual(known['stats']['silver_medals'], 1)
        self.assertEqual(known['stats']['total'], 2)
        self.assertEqual(known['prayers']['asr'], 2)
        self.assertIsNone(data['rows'][-1]['day']['stats']['completed'])
        self.assertEqual(data['generated_at'], '2026-10-03T10:00:00Z')
        self.schedules.calls.clear()
        future = (await self.report('date=2026-10-04')).json()
        by_id = {row['student']['id']: row['day'] for row in future['rows']}
        self.assertEqual(by_id[5]['stats']['total'], 5)  # Existing no-city fallback.
        self.assertEqual(by_id[2]['stats']['total'], 0)
        self.assertEqual(self.schedules.calls, [])

    async def test_revocation_reconciles_full_roster_before_pagination(self):
        for user_id in (2, 3):
            self.add_user(user_id, f'child-{user_id}', 'jeddah')
        def unlink():
            with self.app.state.database.connect() as conn:
                conn.execute('DELETE FROM supervisor_children WHERE child_id=2')
        self.schedules.callback = unlink
        data = (await self.report('date=2026-10-03&limit=1')).json()
        self.assertEqual(data['total_students'], 1)
        self.assertEqual(data['rows'][0]['student']['id'], 3)
        def revoke():
            with self.app.state.database.connect() as conn:
                conn.execute('UPDATE users SET can_supervise=0 WHERE id=1')
        self.schedules.callback = revoke
        self.assertEqual((await self.report('date=2026-10-03')).status_code, 403)

    async def test_one_instant_controls_different_local_dates(self):
        self.mock_clock.now.return_value = datetime(2026, 12, 2, 21, 30, tzinfo=timezone.utc)
        self.add_user(2, 'child-saudi', 'jeddah')
        self.add_user(3, 'child-egypt', 'cairo')
        # Simulate a slow schedule request completing after the clock advances.
        self.schedules.callback = lambda: setattr(
            self.mock_clock.now, 'return_value', datetime(2026, 12, 3, 10, tzinfo=timezone.utc))
        data = (await self.report('date=2026-12-03')).json()
        by_id = {row['student']['id']: row['day'] for row in data['rows']}
        self.assertEqual(by_id[2]['schedule']['today'], '2026-12-03')
        self.assertEqual(by_id[3]['schedule']['today'], '2026-12-02')
        self.assertEqual(by_id[2]['stats']['total'], 0)
        self.assertEqual(by_id[3]['stats']['total'], 0)
        self.assertEqual(self.schedules.calls, [('jeddah', '2026-12-03')])
        self.assertEqual(data['generated_at'], '2026-12-02T21:30:00Z')
        self.mock_clock.now.assert_called_once_with(timezone.utc)

    async def test_auth_validation_and_empty_roster(self):
        self.assertEqual((await self.report()).json()['rows'], [])
        for query in ('date=2026-02-30', 'date=2026-1-01', 'date=2026-10-02&limit=101',
                      'date=2026-10-02&limit=0', 'date=2026-10-02&offset=-1', 'limit=1'):
            self.assertEqual((await self.report(query)).status_code, 422)
        with self.app.state.database.connect() as conn:
            conn.execute('UPDATE users SET can_supervise=0 WHERE id=1')
        self.assertEqual((await self.report()).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual((await self.report()).status_code, 401)

    async def test_unexpected_schedule_error_is_not_masked(self):
        self.add_user(2, 'child-error', 'jeddah')
        self.schedules.callback = lambda: (_ for _ in ()).throw(RuntimeError('unexpected'))
        with self.assertRaises(RuntimeError):
            await self.report('date=2026-10-03')
