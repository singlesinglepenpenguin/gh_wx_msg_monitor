"""临时目录验证路径发现；不代表其他电脑实测，不调用微信初始化。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from group_msg_monitor.wechat_source import account_paths, discover_accounts


class AccountPathTests(unittest.TestCase):
    def test_manual_account_and_storage_root_return_all_accounts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root/'xwechat_files/wxid_one/db_storage'
            second = root/'xwechat_files/wxid_two/db_storage'
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            expected = sorted([str(first.resolve()), str(second.resolve())])
            self.assertEqual(account_paths(root), expected)
            self.assertEqual(account_paths(root/'xwechat_files'), expected)
            self.assertEqual(account_paths(first.parent), [str(first.resolve())])
            self.assertEqual(account_paths(first), [str(first.resolve())])
            self.assertEqual(account_paths(root/'missing'), [])

    def test_mac_discovery_uses_current_home_and_keeps_multiple_accounts(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            root = home/'Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files'
            accounts = [root/name/'db_storage' for name in ('wxid_other', 'wxid_current')]
            for account in accounts:
                account.mkdir(parents=True)
            with patch('group_msg_monitor.wechat_source.sys.platform', 'darwin'), patch('group_msg_monitor.wechat_source.Path.home', return_value=home):
                self.assertEqual(discover_accounts(), sorted(str(p.resolve()) for p in accounts))

    def test_mac_legacy_deeper_layout_is_found(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            account = home/'Library/Application Support/com.tencent.xinWeChat/legacy/accounts/wxid_test/db_storage'
            account.mkdir(parents=True)
            with patch('group_msg_monitor.wechat_source.sys.platform', 'darwin'), patch('group_msg_monitor.wechat_source.Path.home', return_value=home):
                self.assertEqual(discover_accounts(), [str(account.resolve())])


if __name__ == '__main__':
    unittest.main()
