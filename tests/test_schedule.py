import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from group_msg_monitor.app import App
from group_msg_monitor.relay_core import parse_bjt


class ScheduleTests(unittest.TestCase):
    def make_app(self):
        calls = []
        app = SimpleNamespace(auto=SimpleNamespace(get=lambda: True),
                              schedule_attempt=None, busy=False,
                              _log=lambda s: None, _update=lambda: calls.append('update'),
                              after=lambda milliseconds, fn: calls.append(milliseconds))
        app._schedule = lambda: App._schedule(app)
        return app, calls

    def tick(self, app, value):
        with patch('group_msg_monitor.app.now_bjt', return_value=parse_bjt(value)):
            app._schedule()

    def test_each_daily_slot_runs_once_and_next_day_continues(self):
        app, calls = self.make_app()
        for when in ('2026-10-27 09:00:00', '2026-10-27 09:00:10',
                     '2026-10-27 15:00:00', '2026-10-27 15:04:59',
                     '2026-10-28 09:00:00'):
            self.tick(app, when)
        self.assertEqual(calls.count('update'), 3)
        self.assertEqual(calls.count(10000), 5)

    def test_disabled_busy_and_missed_slots_do_not_start_updates(self):
        app, calls = self.make_app()
        app.auto.get = lambda: False
        self.tick(app, '2026-10-27 09:00:00')
        app.auto.get = lambda: True
        app.busy = True
        self.tick(app, '2026-10-27 09:01:00')
        app.busy = False
        self.tick(app, '2026-10-27 09:05:00')
        self.assertNotIn('update', calls)


if __name__ == '__main__':
    unittest.main()
