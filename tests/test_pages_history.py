from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'pages-history.sh'


class PagesHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / 'remote.git'
        self.repo = self.root / 'app'
        self.run_command('git', 'init', '--bare', str(self.remote), cwd=self.root)
        self.run_command('git', 'init', '-b', 'main', str(self.repo), cwd=self.root)
        self.run_command('git', 'config', 'user.name', 'Test')
        self.run_command('git', 'config', 'user.email', 'test@example.org')
        (self.repo / 'README.md').write_text('Application code')
        self.run_command('git', 'add', 'README.md')
        self.run_command('git', 'commit', '-m', 'Initial')
        self.run_command('git', 'remote', 'add', 'origin', str(self.remote))
        self.run_command('git', 'push', '-u', 'origin', 'main')

    def run_command(self, *args, cwd=None, check=True):
        return subprocess.run(args, cwd=cwd or self.repo, capture_output=True, text=True, check=check)

    def script(self, *args, check=True):
        return self.run_command('bash', str(SCRIPT), *args, check=check)

    def test_bootstrap_persist_restore_and_unchanged_save(self):
        self.assertNotEqual(self.script('restore', check=False).returncode, 0)
        self.script('restore', '--initialize')
        history = self.repo / '.feed-history'
        self.assertFalse((history / 'README.md').exists())
        (history / 'feed.xml').write_text('Persistent feed')
        self.script('save')
        revision = self.run_command('git', '-C', str(self.remote), 'rev-parse', 'feed-data').stdout
        self.assertEqual(self.run_command('git', '-C', str(self.remote), 'ls-tree', '--name-only', 'feed-data').stdout.strip(), 'feed.xml')
        self.script('save')
        self.assertEqual(self.run_command('git', '-C', str(self.remote), 'rev-parse', 'feed-data').stdout, revision)
        self.run_command('git', 'worktree', 'remove', str(history))
        self.script('restore')
        self.assertEqual((history / 'feed.xml').read_text(), 'Persistent feed')

    def test_missing_remote_is_not_treated_as_empty_history(self):
        self.run_command('git', 'remote', 'set-url', 'origin', str(self.root / 'missing.git'))
        self.assertNotEqual(self.script('restore', '--initialize', check=False).returncode, 0)
        self.assertFalse((self.repo / '.feed-history').exists())

    def test_existing_branch_missing_feed_cannot_be_reinitialized(self):
        self.run_command('git', 'push', 'origin', 'HEAD:refs/heads/feed-data')
        result = self.script('restore', '--initialize', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refusing to reset history', result.stderr)
