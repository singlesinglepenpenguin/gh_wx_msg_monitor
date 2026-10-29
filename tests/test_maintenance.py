"""验证上游调用参数及失败传播；使用测试替身，不触发真实抓取或重签名。"""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
from group_msg_monitor.wechat_source import initialize, repair_mac_permission
from group_msg_monitor.error_messages import error_message


class MaintenanceTests(unittest.TestCase):
    def test_normal_initialization_uses_original_scanner(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch('group_msg_monitor.wechat_source.sys.platform', 'darwin'), patch('wechat_native.scanner_mac.extract_keys', return_value={'fixture_salt': 'fixture_key'}) as scan:
                self.assertEqual(initialize(str(root), root/'local_data'), 1)
                scan.assert_called_once_with(str(root))
                self.assertEqual(json.loads((root/'local_data/wechat.json').read_text())['keys'], {'fixture_salt': 'fixture_key'})

    def test_new_initialization_is_original_one_time_40_second_capture(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch('group_msg_monitor.wechat_source.sys.platform', 'darwin'), patch('wechat_native.scanner_mac.extract_db_keys_lldb', return_value=(None, ['fixture_candidate'])) as capture, patch('wechat_native.key_scan.keys_from_enc_candidates', return_value={'fixture_salt': 'fixture_key'}) as verify:
                initialize(str(root), root/'local_data', capture=True)
                capture.assert_called_once_with(timeout=40)
                verify.assert_called_once_with(['fixture_candidate'], str(root))

    def test_empty_scan_does_not_replace_saved_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); local = root/'local_data'; local.mkdir()
            config = local/'wechat.json'; config.write_text('{"existing": true}')
            with patch('group_msg_monitor.wechat_source.sys.platform', 'darwin'), patch('wechat_native.scanner_mac.extract_keys', return_value={}):
                with self.assertRaisesRegex(ValueError, '未取得密钥'):
                    initialize(str(root), local)
            self.assertEqual(config.read_text(), '{"existing": true}')

    def test_permission_repair_preserves_upstream_status_and_error_code(self):
        with patch('wechat_native.scanner_mac.resign_wechat', return_value={'status': 'ok'}) as repair:
            self.assertEqual(repair_mac_permission()['status'], 'ok')
            repair.assert_called_once_with()
        with patch('wechat_native.scanner_mac.resign_wechat', return_value={'status': 'error', 'code': 'WECHAT_RUNNING', 'message': '微信仍在运行'}):
            with self.assertRaises(ValueError) as exc:
                repair_mac_permission()
            self.assertEqual(error_message(exc.exception)[0], 'WECHAT_RUNNING')
