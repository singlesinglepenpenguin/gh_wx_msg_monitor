import sys
import json
import queue
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace, MethodType
import unittest
from unittest.mock import patch, Mock

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from group_msg_monitor.app import App
from group_msg_monitor.relay_core import parse_bjt, load_state


class ScheduleTests(unittest.TestCase):
    def make_app(self):
        calls = []
        app = SimpleNamespace(auto=SimpleNamespace(get=lambda: True),
                              schedule_attempt=None, busy=False,
                              _log=lambda s: None, _update=lambda **kwargs: calls.append('update'),
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

    def test_connection_preparation_defers_until_idle_within_slot(self):
        app, calls = self.make_app()
        app.settings_window = SimpleNamespace(waiting=True, connecting=False)
        self.tick(app, '2026-10-27 09:00:00')
        self.assertNotIn('update', calls)
        app.settings_window.waiting = False
        self.tick(app, '2026-10-27 09:01:00')
        self.assertEqual(calls.count('update'), 1)


class Value:
    def __init__(self, value=''):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class AutomaticExecutionTests(unittest.TestCase):
    """Real worker, event queue and Excel versioning; only WeChat/time are simulated."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.target, self.roster = root/'baseline.xlsx', root/'roster.xlsx'
        for path, rows in ((self.target, [('机构/截止日期', '赵阳'), ('富国基金', '待清空')]),
                           (self.roster, [('姓名',), ('赵阳',)])):
            book = openpyxl.Workbook()
            book.active.title = 'Sheet1'
            for row in rows:
                book.active.append(row)
            book.save(path)
            book.close()
        app = self.app = SimpleNamespace(local_dir=root/'local_data', output_dir=root/'output',
            records_dir=root/'records', events=queue.Queue(), auto=Value(True), busy=False,
            schedule_attempt=None, settings_window=None, run_until=None, connection_ok=False,
            vars={k: Value() for k in ('target', 'roster', 'start', 'current', 'status', 'connection')},
            run_btn=Mock(), file_buttons=[Mock()], path_entries={'target': Mock()},
            log_lines=[], after=Mock(), _show_state=Mock())
        for name in ('_save_preferences', '_log', '_work', '_update', '_poll', '_schedule'):
            setattr(app, name, MethodType(getattr(App, name), app))
        app.vars['target'].set(str(self.target))
        app.vars['roster'].set(str(self.roster))
        app.vars['start'].set('2026-10-27 00:00:00')
        self.fetch = Mock(side_effect=lambda start, end, group: [{'content': '#接龙\n【富国基金】打分\n1. 赵阳 已要',
                           'create_ts': int(end.timestamp()) - 1}])
        factory = patch('group_msg_monitor.app.WeChatSource', return_value=SimpleNamespace(fetch=self.fetch))
        factory.start()
        self.addCleanup(factory.stop)
        self.dialog = patch('group_msg_monitor.app.messagebox.showerror')
        self.showerror = self.dialog.start()
        self.addCleanup(self.dialog.stop)

    def run_slot(self, when):
        with patch('group_msg_monitor.app.now_bjt', return_value=parse_bjt(when)):
            self.app._schedule()
        deadline = time.monotonic() + 5
        while self.app.busy or not self.app.events.empty():
            self.app._poll()
            if time.monotonic() > deadline:
                self.fail('Automatic worker/event queue failed to become idle')
            time.sleep(.005)
        self.assertFalse(self.app.busy)
        self.assertIsNone(self.app.run_until)
        self.app.run_btn.configure.assert_called_with(state='normal')
        self.showerror.assert_not_called()

    def test_morning_afternoon_and_next_day_run_to_completion(self):
        paths = []
        for when in ('2026-10-27 09:00:00', '2026-10-27 15:00:00', '2026-10-28 09:00:00'):
            self.run_slot(when)
            path = Path(self.app.vars['target'].get())
            paths.append(path)
            self.assertTrue(path.is_file())
            self.assertEqual(load_state(path)['through'], parse_bjt(when).isoformat())
        self.assertEqual(len(set(paths)), 3)
        details = sorted(self.app.records_dir.glob('*/details.json'))
        results = [json.loads(p.read_text('utf-8')) for p in details]
        self.assertEqual([r['cleared_count'] for r in results], [1, 0, 0])
        self.assertEqual([r['already_empty_count'] for r in results], [0, 1, 1])
        self.assertEqual(results[1]['source'], str(paths[0]))
        self.assertEqual(results[2]['source'], str(paths[1]))
        self.assertEqual(self.fetch.call_count, 3)
        self.run_slot('2026-10-28 09:00:10')
        self.assertEqual(self.fetch.call_count, 3)

    def test_fetch_failure_does_not_block_afternoon(self):
        successful_fetch = self.fetch.side_effect
        self.fetch.side_effect = RuntimeError('simulated read failure')
        self.run_slot('2026-10-27 09:00:00')
        self.assertFalse(load_state(self.target))
        self.fetch.side_effect = successful_fetch
        self.run_slot('2026-10-27 15:00:00')
        self.assertTrue(load_state(Path(self.app.vars['target'].get())))

    def test_invalid_input_is_nonmodal_and_next_slot_recovers(self):
        self.app.vars['target'].set('missing.xlsx')
        # No worker or disabled controls on preflight failure.
        self.app.run_btn.configure(state='normal')
        self.run_slot('2026-10-27 09:00:00')
        self.fetch.assert_not_called()
        self.app.vars['target'].set(str(self.target))
        self.run_slot('2026-10-27 15:00:00')
        self.assertEqual(self.fetch.call_count, 1)

    def test_event_handler_failure_does_not_stop_queue_polling_or_next_slot(self):
        self.app._show_state.side_effect = ValueError('simulated display failure')
        self.run_slot('2026-10-27 09:00:00')
        self.app._show_state.side_effect = None
        self.run_slot('2026-10-27 15:00:00')
        self.assertEqual(self.fetch.call_count, 2)

    def test_preference_write_failure_does_not_stop_scheduler(self):
        original = self.app._save_preferences
        self.app._save_preferences = Mock(side_effect=OSError('simulated permission failure'))
        self.app.run_btn.configure(state='normal')
        self.run_slot('2026-10-27 09:00:00')
        self.app._save_preferences = original
        self.run_slot('2026-10-27 15:00:00')
        self.assertEqual(self.fetch.call_count, 1)

    def test_thread_start_failure_releases_busy_and_next_slot_runs(self):
        with patch('group_msg_monitor.app.threading.Thread.start', side_effect=RuntimeError('simulated thread failure')):
            self.run_slot('2026-10-27 09:00:00')
        self.run_slot('2026-10-27 15:00:00')
        self.assertEqual(self.fetch.call_count, 1)

    def test_locked_excel_skips_run_without_advancing_then_next_slot_recovers(self):
        marker = self.target.with_name('~$' + self.target.name)
        marker.touch()
        self.run_slot('2026-10-27 09:00:00')
        self.assertFalse(load_state(self.target))
        self.fetch.assert_not_called()
        marker.unlink()
        self.run_slot('2026-10-27 15:00:00')
        self.assertEqual(self.fetch.call_count, 1)

    def test_unexpected_scheduler_failure_keeps_timer_alive(self):
        original = self.app._update
        self.app._update = Mock(side_effect=RuntimeError('simulated scheduler callback failure'))
        self.app.run_btn.configure(state='normal')
        self.run_slot('2026-10-27 09:00:00')
        self.app.after.assert_any_call(10000, self.app._schedule)
        self.app._update = original
        self.run_slot('2026-10-27 15:00:00')
        self.assertEqual(self.fetch.call_count, 1)

    def test_manual_validation_error_still_shows_operator_dialog(self):
        self.app.vars['target'].set('missing.xlsx')
        self.app._update()
        self.app._poll()
        self.showerror.assert_called_once()


if __name__ == '__main__':
    unittest.main()
