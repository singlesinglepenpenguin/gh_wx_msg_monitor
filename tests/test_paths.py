from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from group_msg_monitor import paths


class PathTests(unittest.TestCase):
    def test_source_runtime_and_resource_paths_share_project_root(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(paths.PROJECT_ROOT, root)
        self.assertEqual(paths.resource_root(), root)
        self.assertEqual(paths.data_home(), root/'data/local_data')

    def test_mac_bundle_writes_beside_app_and_reads_bundled_resources(self):
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary).resolve()
            binary = release/'GroupMessageMonitor.app/Contents/MacOS/GroupMessageMonitor'
            resources = release/'GroupMessageMonitor.app/Contents/Resources'
            with patch.object(paths.sys, 'frozen', True, create=True), patch.object(paths.sys, 'executable', str(binary)), patch.object(paths.sys, 'platform', 'darwin'), patch.object(paths.sys, '_MEIPASS', str(resources), create=True):
                self.assertEqual(paths.data_home(), release/'data/local_data')
                self.assertEqual(paths.resource_root(), resources)
