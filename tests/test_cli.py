import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from blbl_tracker.__main__ import main
from test_sync import FakeTransport, SOURCE, STATS, STAMP


class CliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name) / 'config.toml'
        self.config.write_text('''[properties]
source="source"
views="views"
likes="likes"
favorites="favorites"
coins="coins"
last_synced="last_synced"
[published]
property="published"
type="checkbox"
value=true
''')

    def test_missing_credentials_fail_before_network_and_do_not_leak(self):
        with patch.dict(os.environ, {}, clear=True), patch('blbl_tracker.__main__.Transport') as transport:
            with self.assertLogs(level='ERROR') as logs:
                self.assertEqual(main(['--config', str(self.config)]), 2)
            transport.assert_not_called()
            self.assertNotIn(str(self.config), '\n'.join(logs.output))

    def test_default_and_explicit_dry_run_and_apply_share_flow(self):
        for flag in ([], ['--dry-run'], ['--apply'], ['--check-schema']):
            transport = FakeTransport()
            with patch.dict(os.environ, {'NOTION_TOKEN': 'test-token', 'NOTION_DATA_SOURCE_ID': SOURCE}), \
                 patch('blbl_tracker.__main__.Transport', return_value=transport), \
                 patch('blbl_tracker.__main__.Bilibili') as bili:
                bili.return_value.fetch.return_value = (STATS, STAMP)
                self.assertEqual(main(['--config', str(self.config), *flag]), 0)
            self.assertEqual(len(transport.patches), int(flag == ['--apply']))
            if flag == ['--check-schema']:
                bili.assert_not_called()

    def test_contradictory_flags_fail(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
            main(['--apply', '--dry-run'])
        self.assertEqual(result.exception.code, 2)

    def test_invalid_toml_fails_safely(self):
        self.config.write_text('NOTION_TOKEN = secret-not-valid-toml')
        with self.assertLogs(level='ERROR') as logs:
            self.assertEqual(main(['--config', str(self.config)]), 2)
        self.assertNotIn('secret-not-valid-toml', '\n'.join(logs.output))
