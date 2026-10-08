"""Doctor imports hook dependencies even when a hook hides its import errors."""
import sys
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
import nth_doctor as doctor


class HookImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='nth-hook-doctor-')
        self.addCleanup(self.tmp.cleanup)
        self.install = Path(self.tmp.name)
        (self.install / 'nth_claude_hook.py').write_text('try:\n import nth_listener\nexcept ImportError:\n pass\n')
        (self.install / 'nth_listener.py').write_text('import nth_notice\nimport nth_sse_client\n')
        (self.install / 'nth_notice.py').write_text('NOTICE = "synthetic"\n')
        (self.install / 'nth_sse_client.py').write_text('CLIENT = True\n')

    def row(self):
        return doctor._hook_import_check(self.install, sys.executable)

    def test_complete_install_imports(self):
        self.assertEqual(self.row()[1], doctor.OK)

    def test_current_native_modules_import(self):
        for p in self.install.glob('*.py'):
            p.unlink()
        for p in Path(doctor.__file__).parent.glob('*.py'):
            shutil.copyfile(p, self.install / p.name)
        row = self.row()
        self.assertEqual(row[1], doctor.OK, row[2])

    def test_each_missing_shared_module_is_named(self):
        for name in ('nth_listener', 'nth_notice', 'nth_sse_client'):
            with self.subTest(module=name):
                p = self.install / (name + '.py')
                contents = p.read_text()
                p.unlink()
                try:
                    row = self.row()
                    self.assertEqual(row[1], doctor.FAIL)
                    self.assertIn(name, row[2])
                finally:
                    p.write_text(contents)

    def test_present_shared_module_is_checked_even_without_hook_import(self):
        (self.install / 'nth_claude_hook.py').write_text('# imports are lazy\n')
        (self.install / 'nth_notice.py').write_text('import missing_hook_dependency\n')
        row = self.row()
        self.assertEqual(row[1], doctor.FAIL)
        self.assertIn('missing_hook_dependency', row[2])

    def test_old_install_without_hooks_has_no_check(self):
        (self.install / 'nth_claude_hook.py').unlink()
        self.assertIsNone(self.row())

    def test_check_is_wired_into_doctor(self):
        with patch.object(doctor, '_read_registration', return_value=(None, None)), \
             patch.object(doctor, 'INSTALL_DIR', self.install), \
             patch.object(doctor, 'HUB_INSTALL_DIR', self.install / 'no-hub'), \
             patch.object(doctor, 'DB_PATH', self.install / 'no.db'), \
             patch.object(doctor, '_installed_version', return_value=('test', self.install)), \
             patch.object(doctor, '_freshness_check', return_value=None), \
             patch.object(doctor, '_http_json', return_value=(None, None, 'offline')):
            (self.install / 'nth_notice.py').unlink()
            checks, _ = doctor.run_checks()
        self.assertTrue(any(label == 'hook import' and level == doctor.FAIL and 'nth_notice' in detail
                            for label, level, detail in checks))


if __name__ == '__main__':
    unittest.main()
