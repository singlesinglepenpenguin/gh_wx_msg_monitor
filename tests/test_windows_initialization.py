"""Control-flow tests with mocked upstream calls; these do not read real WeChat."""
from pathlib import Path
import os
import subprocess
import sys
import unittest
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from group_msg_monitor.wechat_source import _initialize_windows


class WindowsInitializationTests(unittest.TestCase):
    def test_script_failure_preserves_exit_code_and_redacts_console(self):
        from wechat_native import active_key
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(active_key.tempfile, 'gettempdir', return_value=temp), \
             patch.object(active_key, 'is_admin', return_value=True), \
             patch.object(active_key.subprocess, 'run', return_value=SimpleNamespace(returncode=7, stdout='failure ' + 'ab' * 32)):
            detail = active_key._run_active(Path(temp) / 'fixture.ps1', [], 1)
            self.assertIn('exit_code=7', detail)
            self.assertNotIn('ab' * 32, detail)
            self.assertEqual(list(Path(temp).iterdir()), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows CNG only')
    def test_native_kdf_equals_original_managed_backend(self):
        script = Path(__file__).resolve().parents[1] / 'scripts/verify_windows_kdf.ps1'
        result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)],
                                capture_output=True, text=True, errors='replace', timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_active_first_and_secrets_redacted(self):
        secret = 'ab' * 32
        lines = []
        def active(directory, on_log):
            on_log('master key: ' + secret)
            return {'salt': 'fixture'}
        with patch('wechat_native.scanner_win.extract_keys_active', side_effect=active), \
             patch('wechat_native.scanner_win.extract_keys') as passive:
            self.assertEqual(_initialize_windows('selected/db_storage', lines.append), {'salt': 'fixture'})
            passive.assert_not_called()
        self.assertNotIn(secret, '\n'.join(lines))

    def test_running_wechat_never_falls_back(self):
        with patch('wechat_native.scanner_win.extract_keys_active', side_effect=RuntimeError('检测到微信正在运行 (PID: 123)')), \
             patch('wechat_native.scanner_win.extract_keys') as passive:
            with self.assertRaisesRegex(RuntimeError, '检测到微信正在运行'):
                _initialize_windows('selected/db_storage', lambda _: None)
            passive.assert_not_called()

    def test_upstream_error_falls_back_and_preserves_stage(self):
        lines = []
        with patch('wechat_native.scanner_win.extract_keys_active', side_effect=RuntimeError('fixture failure')), \
             patch('wechat_native.scanner_win.extract_keys', return_value={}) as passive:
            self.assertEqual(_initialize_windows('selected/db_storage', lines.append), {})
            self.assertEqual(passive.call_args.args, ('selected/db_storage',))
        self.assertIn('fixture failure', '\n'.join(lines))

    def test_fallback_failure_keeps_original_active_error(self):
        with patch('wechat_native.scanner_win.extract_keys_active', side_effect=RuntimeError('original active failure')), \
             patch('wechat_native.scanner_win.extract_keys', side_effect=RuntimeError('Weixin.exe 未运行')):
            with self.assertRaisesRegex(RuntimeError, 'WIN_ACTIVE_FAILED: original active failure'):
                _initialize_windows('selected/db_storage', lambda _: None)

    def test_vendor_manifest_has_no_unrecorded_files_or_changes(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
        from verify_vendor import verify
        self.assertEqual(verify()['differences'], [])


if __name__ == '__main__':
    unittest.main()
