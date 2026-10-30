"""Metadata and GUI routing tests; no real client shutdown or key extraction."""
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from group_msg_monitor.windows_client import account_in_use
from group_msg_monitor.connection_wizard import WindowsConnection
from group_msg_monitor.wechat_source import WeChatSource


class WindowsClientTests(unittest.TestCase):
    def test_first_connection_reopens_client_normally_after_key_verification(self):
        from group_msg_monitor.connection_wizard import connect_and_verify
        order = []
        with patch('group_msg_monitor.connection_wizard.initialize', side_effect=lambda *a, **k: order.append('initialize')) as init, \
                patch('group_msg_monitor.connection_wizard.WeChatSource') as source, \
                patch('group_msg_monitor.connection_wizard.open_wechat', side_effect=lambda: order.append('normal_open')):
            source.return_value.inspect.side_effect = lambda: order.append('verified') or {'group': 'target'}
            self.assertEqual(connect_and_verify('selected', 'local', lambda _: None), {'group': 'target'})
            init.assert_called_once_with('selected', 'local', unittest.mock.ANY, capture=True)
        self.assertEqual(order, ['initialize', 'verified', 'normal_open'])

    def test_account_requires_a_file_handle_owned_by_weixin(self):
        accounts = ['old/db_storage', 'current/db_storage']
        def users(path):
            return [555] if str(path).startswith('old') else [123]
        with patch('wechat_native.scanner_win._list_running_weixin_pids', return_value=[123]), \
                patch('group_msg_monitor.windows_client.file_users', side_effect=users), \
                patch.object(Path, 'is_file', return_value=True):
            self.assertEqual(account_in_use(accounts), [str(Path('current/db_storage'))])

    def test_current_account_wins_over_directory_order(self):
        selected = Mock()
        wizard = SimpleNamespace(connecting=False, waiting=False, app=SimpleNamespace(busy=False, local_dir='unused', connection_ok=False),
                                 entries={}, picker=Mock(), selected=selected, status=Mock(), stage=Mock(), detail=Mock())
        accounts = ['old/db_storage', 'current/db_storage']
        with patch('group_msg_monitor.connection_wizard.discover_accounts', return_value=accounts), \
                patch('group_msg_monitor.connection_wizard.account_in_use', return_value=[accounts[1]]), \
                patch('group_msg_monitor.connection_wizard.WeChatSource') as source:
            source.return_value.config.side_effect = ValueError('no credentials')
            WindowsConnection.identify(wizard)
        label = selected.set.call_args.args[0]
        self.assertEqual(wizard.entries[label], accounts[1])

    def test_ambiguous_accounts_never_silently_choose_first(self):
        wizard = SimpleNamespace(connecting=False, waiting=False, app=SimpleNamespace(busy=False, local_dir='unused', connection_ok=False),
                                 entries={}, picker=Mock(), selected=Mock(), status=Mock(), stage=Mock(), detail=Mock())
        with patch('group_msg_monitor.connection_wizard.discover_accounts', return_value=['a/db_storage', 'b/db_storage']), \
                patch('group_msg_monitor.connection_wizard.account_in_use', return_value=[]), \
                patch('group_msg_monitor.connection_wizard.WeChatSource') as source:
            source.return_value.config.side_effect = ValueError('no credentials')
            WindowsConnection.identify(wizard)
        wizard.selected.set.assert_called_once_with('')

    @unittest.skipUnless(sys.platform == 'win32', 'Windows fresh-start behavior')
    def test_missing_keys_are_not_silently_imported_from_external_gh_ui(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory, \
                patch('group_msg_monitor.wechat_source.import_gh_config') as imported:
            with self.assertRaisesRegex(ValueError, '尚未连接微信'):
                WeChatSource(Path(directory)).config()
            imported.assert_not_called()


if __name__ == '__main__':
    unittest.main()
