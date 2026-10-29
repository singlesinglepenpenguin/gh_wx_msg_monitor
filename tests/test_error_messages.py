import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from group_msg_monitor.error_messages import error_message, record_error


class ErrorMessagesTests(unittest.TestCase):
    def test_missing_helper_and_denied_helper_have_different_actions(self):
        missing = RuntimeError("can't open file '\"/tmp/_mac_helper.py\"': No such file or directory")
        denied = RuntimeError("can't open file '/tmp/_mac_helper.py': Operation not permitted")
        self.assertEqual(error_message(missing)[0], 'HELPER_PATH')
        self.assertEqual(error_message(denied)[0], 'SYSTEM_FILE_ACCESS')

    def test_capture_failure_does_not_claim_group_is_empty(self):
        code, summary, action = error_message(RuntimeError('CAPTURE_FAILED: 未抓到密钥'))
        self.assertEqual(code, 'KEY_CAPTURE')
        self.assertIn('一次性', action)
        self.assertNotIn('群没有数据', summary)

    def test_local_records_append_and_redact_exception_and_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = 'ab' * 32
            try:
                raise RuntimeError('candidate=' + secret)
            except RuntimeError as exc:
                report = record_error(directory, exc, '初始化')
            record_error(directory, ValueError('另一条原始错误'), '测试群连接')
            path = Path(report['record_path'])
            raw = path.read_text('utf-8')
            self.assertNotIn(secret, raw)
            rows = [json.loads(line) for line in raw.splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertIn('RuntimeError', rows[0]['traceback'])
            self.assertEqual(rows[1]['error'], '另一条原始错误')
            mirrored = Path(report['diagnostic_path']).read_text('utf-8')
            self.assertNotIn(secret, mirrored)
            self.assertEqual(len(mirrored.splitlines()), 2)
            if os.name != 'nt':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_record_failure_preserves_original_error(self):
        with tempfile.TemporaryDirectory() as directory:
            file_path = Path(directory) / 'not_a_directory'
            file_path.write_text('occupied')
            report = record_error(file_path, RuntimeError('原始故障'), '测试群连接')
            self.assertEqual(report['error'], '原始故障')
            self.assertIn('record_error', report)
            self.assertNotIn('record_path', report)


if __name__ == '__main__':
    unittest.main()
