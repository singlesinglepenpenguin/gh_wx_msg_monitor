from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from verify_distribution import verify


class DistributionTests(unittest.TestCase):
    def check_files(self, files):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / 'package.zip'
            with zipfile.ZipFile(archive, 'w') as bundle:
                for name, text in files.items():
                    bundle.writestr('GroupMessageMonitor/' + name, text)
            return verify(archive)

    def test_generic_customer_guide_and_upstream_link_are_allowed(self):
        result = self.check_files({'user_manual.md': 'Use setup.bat. https://github.com/example/project',
                                   '_internal/data/diagnostics/error_message.md': 'Generic error guidance.'})
        self.assertEqual(result['status'], 'passed')

    def test_runtime_credentials_workbooks_and_logs_are_rejected(self):
        for name in ('data/local_data/wechat.json', 'reference/sample.xlsx', 'runtime.log', 'error_message.jsonl'):
            with self.subTest(name=name):
                self.assertEqual(self.check_files({name: 'fixture'})['status'], 'failed')

    def test_personal_documentation_is_rejected_without_echoing_its_contents(self):
        for text in ('wxid_fixture_account', 'D:/private/account', '/Users/fixture/private'):
            with self.subTest(text=text):
                result = self.check_files({'_internal/data/diagnostics/error_message.md': text})
                self.assertEqual(result['status'], 'failed')
                self.assertNotIn(text, str(result['errors']))
