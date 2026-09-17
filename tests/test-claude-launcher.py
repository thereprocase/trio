"""`trio claude` launcher invariants; isolated NTH_HOME, nothing is executed."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import nth_cli

FLAG = '--dangerously-load-development-channels'


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {'NTH_HOME': self.temp.name})
        self.environment.start()
        os.environ.pop('TRIO_CLAUDE_CHANNEL', None)

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def configure(self, **values):
        (self.home / 'native.json').write_text(json.dumps(values), encoding='utf-8')

    def launch(self, *arguments):
        with patch.object(nth_cli.shutil, 'which', return_value='claude'), \
             patch.object(nth_cli.subprocess, 'call', return_value=0) as call, \
             patch.object(nth_cli, 'ensure_service') as service, \
             patch.object(nth_cli, 'ensure_codex') as codex:
            self.assertEqual(nth_cli.main(['claude', *arguments]), 0)
        # Channel delivery lives in Claude's own MCP frontends.
        service.assert_not_called()
        codex.assert_not_called()
        return call.call_args

    def test_flag_goes_last_and_names_only_configured_servers(self):
        self.configure()
        command = self.launch('--model', 'chosen-model', 'a prompt').args[0]
        self.assertEqual(command, ['claude', '--model', 'chosen-model', 'a prompt', FLAG, 'server:nth-trio'])
        self.configure(quartet_url='http://hub.example/sse')
        command = self.launch('--resume', 'some-id').args[0]
        self.assertEqual(command, ['claude', '--resume', 'some-id', FLAG, 'server:nth-trio', 'server:nth-qweb'])

    def test_separator_is_dropped_and_no_arguments_is_valid(self):
        self.configure()
        self.assertEqual(self.launch('--', '--continue').args[0], ['claude', '--continue', FLAG, 'server:nth-trio'])
        self.assertEqual(self.launch().args[0], ['claude', FLAG, 'server:nth-trio'])

    def test_environment_marks_the_session_and_keeps_the_callers_variables(self):
        self.configure()
        with patch.dict(os.environ, {'UNRELATED_SETTING': 'kept'}):
            environment = self.launch().kwargs['env']
        self.assertEqual(environment['TRIO_CLAUDE_CHANNEL'], '1')
        self.assertEqual(environment['UNRELATED_SETTING'], 'kept')
        # The launcher's own process is not marked: only the session it starts.
        self.assertNotIn('TRIO_CLAUDE_CHANNEL', os.environ)

    def test_saved_binary_wins_and_a_missing_cli_is_reported(self):
        self.configure(claude_binary='/opt/claude/bin/claude')
        self.assertEqual(self.launch().args[0][0], '/opt/claude/bin/claude')
        self.configure()
        with patch.object(nth_cli.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'Claude Code is not installed'):
                nth_cli.main(['claude'])

    def test_it_never_bypasses_permission_prompts(self):
        self.configure(quartet_url='http://hub.example/sse')
        command = self.launch().args[0]
        self.assertNotIn('--dangerously-skip-permissions', command)
        self.assertEqual([part for part in command if part.startswith('--dangerously')], [FLAG])


if __name__ == '__main__':
    unittest.main()
