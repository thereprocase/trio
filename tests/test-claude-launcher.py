"""`trio claude` launcher invariants; isolated NTH_HOME and Claude config, nothing is executed."""
import io
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
        self.scripts = self.home / 'server'
        self.scripts.mkdir()
        for script in ('nth_server.py', 'nth_quartet_proxy.py'):
            (self.scripts / script).write_text('# placeholder\n')
        self.environment = patch.dict(os.environ, {'NTH_HOME': self.temp.name,
                                                   'CLAUDE_CONFIG_DIR': self.temp.name})
        self.environment.start()
        os.environ.pop('TRIO_CLAUDE_CHANNEL', None)
        # The launcher accepts only its own installation's frontends.
        self.installation = patch.object(nth_cli, 'frontend_root', return_value=self.scripts)
        self.installation.start()
        self.register()

    def tearDown(self):
        self.installation.stop()
        self.environment.stop()
        self.temp.cleanup()

    def configure(self, **values):
        (self.home / 'native.json').write_text(json.dumps(values), encoding='utf-8')

    def imposter(self):
        elsewhere = self.home / 'elsewhere'
        elsewhere.mkdir(exist_ok=True)
        (elsewhere / 'nth_server.py').write_text('# same name, not Trio\n')
        return elsewhere / 'nth_server.py'

    def stdio(self, script, **env):
        return {'type': 'stdio', 'command': sys.executable, 'args': [str(self.scripts / script)],
                'env': dict({'TRIO_NATIVE_CLIENT': 'claude'}, **env)}

    def register(self, **servers):
        """Claude's own MCP configuration, as setup.py writes it unless overridden."""
        registered = {'nth-trio': self.stdio('nth_server.py'),
                      'nth-qweb': dict(self.stdio('nth_quartet_proxy.py'),
                                       args=[str(self.scripts / 'nth_quartet_proxy.py'), '--url',
                                             'http://hub.example/sse'])}
        registered.update(servers)
        registered = {name: value for name, value in registered.items() if value is not None}
        (self.home / '.claude.json').write_text(json.dumps({'mcpServers': registered}), encoding='utf-8')

    def launch(self, *arguments, executable='claude'):
        with patch.object(nth_cli.shutil, 'which', return_value=executable), \
             patch.object(nth_cli.subprocess, 'call', return_value=0) as call, \
             patch.object(nth_cli, 'ensure_service') as service, \
             patch.object(nth_cli, 'ensure_codex') as codex:
            self.assertEqual(nth_cli.main(['claude', *arguments]), 0)
        # Channel delivery lives in Claude's own MCP frontends.
        service.assert_not_called()
        codex.assert_not_called()
        return call.call_args

    def refused(self, *arguments, executable='claude', error=RuntimeError):
        with patch.object(nth_cli.shutil, 'which', return_value=executable), \
             patch.object(nth_cli.subprocess, 'call', return_value=0) as call:
            with self.assertRaises(error) as raised:
                nth_cli.main(['claude', *arguments])
        call.assert_not_called()
        return str(raised.exception)

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

    def test_a_users_own_development_channels_join_the_same_list(self):
        self.configure(quartet_url='http://hub.example/sse')
        command = self.launch(FLAG, 'server:my-own', '--model', 'chosen-model').args[0]
        self.assertEqual(command.count(FLAG), 1)
        self.assertEqual(command, ['claude', FLAG, 'server:nth-trio', 'server:nth-qweb', 'server:my-own',
                                   '--model', 'chosen-model'])

    def test_environment_marks_the_session_and_keeps_the_callers_variables(self):
        self.configure()
        with patch.dict(os.environ, {'UNRELATED_SETTING': 'kept', 'TRIO_CLAUDE_CHANNEL': 'unavailable'}):
            environment = self.launch().kwargs['env']
        self.assertEqual(environment['TRIO_CLAUDE_CHANNEL'], '1')
        self.assertEqual(environment['UNRELATED_SETTING'], 'kept')
        # The launcher's own process is not marked: only the session it starts.
        self.assertNotIn('TRIO_CLAUDE_CHANNEL', os.environ)

    def test_a_network_server_is_never_named_as_a_channel(self):
        # What `setup.sh spoke` leaves behind: nth-qweb pointing straight at the hub.
        self.configure(quartet_url='http://hub.example/sse')
        self.register(**{'nth-qweb': {'type': 'sse', 'url': 'http://hub.example/sse'}})
        with patch('sys.stderr', new_callable=io.StringIO) as stderr:
            command = self.launch().args[0]
        self.assertEqual(command, ['claude', FLAG, 'server:nth-trio'])
        self.assertIn('nth-qweb is registered as a remote (sse) server', stderr.getvalue())
        self.assertIn('will NOT be pushed', stderr.getvalue())

    def test_a_trio_frontend_that_could_never_push_is_refused_with_the_fix(self):
        self.configure()
        cases = (({'nth-trio': dict(self.stdio('nth_server.py'), env={})}, 'lacks TRIO_NATIVE_CLIENT=claude'),
                 ({'nth-trio': None}, 'is not registered for Claude'),
                 ({'nth-trio': {'type': 'http', 'url': 'http://hub.example/mcp'}}, 'remote (http) server'),
                 ({'nth-trio': dict(self.stdio('nth_server.py'), args=['/nowhere/nth_server.py'])},
                  'does not run this installation\'s nth_server.py'),
                 # A real file with the right name, but not this installation's frontend.
                 ({'nth-trio': dict(self.stdio('nth_server.py'), args=[str(self.imposter())])},
                  'does not run this installation\'s nth_server.py'),
                 # The right script as a later argument proves nothing about what runs.
                 ({'nth-trio': dict(self.stdio('nth_server.py'),
                                    args=['-c', 'pass', str(self.scripts / 'nth_server.py')])},
                  'does not run this installation\'s nth_server.py'),
                 ({'nth-trio': dict(self.stdio('nth_server.py'), command='unrelated-command')},
                  'is not started by a Python interpreter'))
        for servers, reason in cases:
            with self.subTest(reason=reason):
                self.register(**servers)
                message = self.refused()
                self.assertIn(reason, message)
                self.assertIn('python setup.py install', message)
        (self.home / '.claude.json').write_text('{not json', encoding='utf-8')
        self.assertIn('Could not read Claude\'s MCP configuration', self.refused())

    def test_saved_binary_wins_and_a_missing_or_stale_one_is_explained(self):
        saved = self.home / 'claude-cli'
        saved.write_text('placeholder')
        self.configure(claude_binary=str(saved))
        self.assertEqual(self.launch().args[0][0], str(saved))
        self.configure(claude_binary=str(self.home / 'moved-away'))
        with patch.object(nth_cli.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'does not exist.*--claude-binary'):
                nth_cli.main(['claude'])
        self.configure()
        with patch.object(nth_cli.shutil, 'which', return_value=None):
            # Installed but off PATH is the common case: name the way out.
            with self.assertRaisesRegex(RuntimeError, 'not found on PATH.*--claude-binary'):
                nth_cli.main(['claude'])

    def test_a_byte_order_mark_in_native_json_is_tolerated(self):
        (self.home / 'native.json').write_bytes(b'\xef\xbb\xbf' + json.dumps({'quartet_url': 'http://hub.example/sse'}).encode())
        self.assertEqual(self.launch().args[0][-2:], ['server:nth-trio', 'server:nth-qweb'])

    def test_a_windows_command_shim_never_receives_shell_metacharacters(self):
        guard = nth_cli.guard_command_shim
        for argument in ('fix a&b', 'a | b', '100%', 'say "hi"', 'x > y', 'caret^', 'two\nlines'):
            with self.assertRaisesRegex(ValueError, r'\.cmd launcher.*--claude-binary'):
                guard('C:/npm/claude.cmd', ['--model', 'chosen-model', argument], platform='nt')
        # Plain arguments are fine through a shim, anything is fine through a real
        # executable, and without cmd.exe nothing reinterprets anything.
        self.assertIsNone(guard('C:/npm/claude.CMD', ['--continue'], platform='nt'))
        self.assertIsNone(guard('C:/bin/claude.exe', ['fix a&b'], platform='nt'))
        self.assertIsNone(guard('/usr/bin/claude.cmd', ['fix a&b'], platform='posix'))
        # The launcher applies it on the platform it runs on.
        self.configure()
        if os.name == 'nt':
            self.assertIn('.cmd launcher', self.refused('fix a&b', executable='C:/npm/claude.cmd', error=ValueError))
        self.assertEqual(self.launch('fix a&b', executable='claude').args[0][1], 'fix a&b')

    def test_it_never_bypasses_permission_prompts(self):
        self.configure(quartet_url='http://hub.example/sse')
        command = self.launch().args[0]
        self.assertNotIn('--dangerously-skip-permissions', command)
        self.assertEqual([part for part in command if part.startswith('--dangerously')], [FLAG])


if __name__ == '__main__':
    unittest.main()
