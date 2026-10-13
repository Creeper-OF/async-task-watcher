#!/usr/bin/env python3
"""Install only the curated skill payload; never overwrite an existing skill."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
INSTALL = REPO / 'scripts/install.py'


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='atw-install-test-')
        self.target = Path(self.tmp.name) / 'skills/async-task-watcher'

    def tearDown(self):
        self.tmp.cleanup()

    def install(self, target=None):
        return subprocess.run([sys.executable, str(INSTALL), '--target', str(target or self.target)],
                              capture_output=True, text=True, timeout=10)

    def test_fresh_install_is_complete_and_supervisor_runs(self):
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        files = {str(p.relative_to(self.target)) for p in self.target.rglob('*') if p.is_file()}
        self.assertEqual(files, {'SKILL.md', 'agents/openai.yaml', 'references/design.md', 'scripts/supervisor.py'})
        supervisor = self.target / 'scripts/supervisor.py'
        r = subprocess.run([sys.executable, str(supervisor), '--root', str(Path(self.tmp.name) / 'state'),
                            'run', '--cwd', self.tmp.name, '--timeout', '5', '--', sys.executable,
                            '-c', 'print("installed-package-ok")'],capture_output=True,text=True,timeout=10)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout)['stdout_tail'], 'installed-package-ok\n')

    def test_existing_skill_is_not_overwritten(self):
        self.target.mkdir(parents=True)
        (self.target / 'SKILL.md').write_text('user-owned content')
        r = self.install()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout)['error'], 'target_exists')
        self.assertEqual((self.target / 'SKILL.md').read_text(), 'user-owned content')
        self.assertEqual(len(list(self.target.iterdir())), 1)

    def test_target_must_be_a_named_skill_directory(self):
        broad = Path(self.tmp.name) / 'skills'
        r = self.install(broad)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout)['error'], 'invalid_target')
        self.assertFalse(broad.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
