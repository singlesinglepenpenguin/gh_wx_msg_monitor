from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch
import tempfile
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from group_msg_monitor.app import App, result_summary
from group_msg_monitor.relay_core import state_path


class AppFlowTests(unittest.TestCase):
    def test_automatic_monitoring_choice_survives_restart(self):
        class Value:
            def __init__(self, value):
                self.value = value
            def get(self):
                return self.value
            def set(self, value):
                self.value = value
        with tempfile.TemporaryDirectory() as temp:
            app = SimpleNamespace(local_dir=Path(temp), auto=Value(True),
                                  vars={k: Value('') for k in ('roster', 'target', 'start')})
            App._save_preferences(app)
            restarted = SimpleNamespace(local_dir=Path(temp), auto=Value(False),
                                        vars={k: Value('') for k in ('roster', 'target', 'start')},
                                        _show_state=lambda: None)
            App._load_preferences(restarted)
            self.assertTrue(restarted.auto.get())

    def test_result_buttons_use_fixed_folders_and_latest_generated_workbook(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); outputs = root/'output'; outputs.mkdir()
            for name, through in [('older.xlsx', '2026-10-28T10:00:00+08:00'), ('latest.xlsx', '2026-10-29T10:00:00+08:00')]:
                (outputs/name).write_bytes(b'path selection fixture')
                state_path(outputs/name).write_text(json.dumps({'through': through}))
            app = SimpleNamespace(output_dir=outputs)
            with patch('group_msg_monitor.app.open_path') as opened:
                App._open_target(app)
                opened.assert_called_once_with(outputs/'latest.xlsx')
            with patch('group_msg_monitor.app.open_path') as opened:
                App._open_folder(app)
                opened.assert_called_once_with(outputs)

    def test_startup_checks_saved_credentials_without_initialization(self):
        events = []
        app = SimpleNamespace(local_dir=Path('/tmp/test-local'))
        app._work = lambda fn, kind, operation: events.append((kind, fn()))
        with patch('group_msg_monitor.app.WeChatSource') as source, patch('group_msg_monitor.app.initialize') as init, patch('group_msg_monitor.app.repair_mac_permission') as repair:
            source.return_value.inspect.return_value = {'group': 'GH派点反馈群'}
            App._check_connection(app)
            source.return_value.inspect.assert_called_once_with()
            init.assert_not_called()
            repair.assert_not_called()
        self.assertEqual(events, [('checked', {'group': 'GH派点反馈群'})])

    def test_summary_merges_duplicate_issues_for_operator(self):
        issue = {'reason': '通讯录同名人员需核对', 'person': '测试姓名', 'detail': '测试姓名 已要'}
        text = result_summary({'relay_count': 69, 'cleared_count': 48, 'already_empty_count': 45, 'issues': [issue, issue.copy()]})
        self.assertIn('清空 48 格', text)
        self.assertIn('1 项待核对', text)
        self.assertNotIn('2 项待核对', text)

    def test_old_missing_people_warnings_are_hidden(self):
        text = result_summary({'relay_count': 1, 'cleared_count': 0, 'already_empty_count': 0,
                               'issues': [{'reason': '目标表没有此人列', 'person': '测试姓名'},
                                          {'reason': '通讯录未匹配姓名', 'detail': '不存在的姓名'}]})
        self.assertNotIn('待核对', text)


if __name__ == '__main__':
    unittest.main()
