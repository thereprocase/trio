"""`trio claude` launcher invariants; isolated NTH_HOME and Claude config, nothing is executed."""
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
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
        # A test runner has no terminal; a person starting a session does.
        self.terminal = patch.object(nth_cli, 'terminal_attached', return_value=True)
        self.terminal.start()
        # The directory the session is started from, with nothing registered in or above it.
        self.project = self.home / 'projects' / 'checkout'
        (self.project / 'nested' / 'deeper').mkdir(parents=True)
        self.directory = patch.object(nth_cli, 'launch_directory', return_value=self.project)
        self.directory.start()
        self.register()

    def tearDown(self):
        self.directory.stop()
        self.terminal.stop()
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

    def register(self, projects=None, **servers):
        """Claude's own MCP configuration, as setup.py writes it unless overridden.
        `projects` is its local scope: {directory: {server name: registration}}."""
        registered = {'nth-trio': self.stdio('nth_server.py'),
                      'nth-qweb': dict(self.stdio('nth_quartet_proxy.py'),
                                       args=[str(self.scripts / 'nth_quartet_proxy.py'), '--url',
                                             'http://hub.example/sse'])}
        registered.update(servers)
        registered = {name: value for name, value in registered.items() if value is not None}
        config = {'mcpServers': registered,
                  'projects': {str(directory): {'mcpServers': local} for directory, local in (projects or {}).items()}}
        (self.home / '.claude.json').write_text(json.dumps(config), encoding='utf-8')

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

    def test_a_same_name_registration_in_a_higher_scope_is_checked_too(self):
        # The flag grants by name, and Claude Code resolves a name local scope first,
        # then the project's .mcp.json, then user scope. Checking only the installed,
        # user-scoped entry would hand the grant to whatever shadows it.
        self.configure(quartet_url='http://hub.example/sse')
        remote = {'type': 'http', 'url': 'http://hub.example/mcp'}
        as_claude_writes_it = self.project.as_posix()
        for label, projects in (('local scope', {as_claude_writes_it: {'nth-trio': remote}}),
                                ('local scope of an ancestor', {str(self.project.parent): {'nth-trio': remote}})):
            with self.subTest(shadow=label):
                self.register(projects=projects)
                message = self.refused()
                self.assertIn('is also registered in local scope for', message)
                self.assertIn('remote (http) server', message)
                self.assertIn('claude mcp remove --scope local nth-trio', message)
                # Reinstalling does not remove it, so that advice would mislead.
                self.assertNotIn('setup.py install', message)
        self.register()
        self.assertEqual(self.launch().args[0][-2:], ['server:nth-trio', 'server:nth-qweb'])
        for label, holder in (('the directory', self.project), ('an ancestor', self.project.parent)):
            with self.subTest(project_file=label):
                (holder / '.mcp.json').write_text(json.dumps({'mcpServers': {'nth-trio': remote}}), encoding='utf-8')
                with patch.object(nth_cli, 'launch_directory', return_value=self.project / 'nested' / 'deeper'):
                    message = self.refused()
                self.assertIn(str(holder / '.mcp.json'), message)
                self.assertIn('remote (http) server', message)
                (holder / '.mcp.json').unlink()
        # A project file that cannot be parsed but names the server cannot be checked.
        (self.project / '.mcp.json').write_text('{"mcpServers": {"nth-trio": {broken', encoding='utf-8')
        self.assertIn('could not be read', self.refused())
        (self.project / '.mcp.json').write_text('{"mcpServers": {"other": {broken', encoding='utf-8')
        self.assertEqual(self.launch().args[0][-2:], ['server:nth-trio', 'server:nth-qweb'])
        (self.project / '.mcp.json').unlink()

    def test_a_shadow_that_is_harmless_or_elsewhere_changes_nothing(self):
        self.configure(quartet_url='http://hub.example/sse')
        remote = {'type': 'http', 'url': 'http://hub.example/mcp'}
        # Another project's local scope, and a same-name entry that IS this installation's frontend.
        self.register(projects={str(self.home / 'projects' / 'another'): {'nth-trio': remote},
                                str(self.project): {'nth-trio': self.stdio('nth_server.py')}})
        (self.project / '.mcp.json').write_text(json.dumps({'mcpServers': {'unrelated': remote}}), encoding='utf-8')
        self.assertEqual(self.launch().args[0][-2:], ['server:nth-trio', 'server:nth-qweb'])
        # A shadowed Quartet frontend costs Quartet delivery, not the session.
        self.register(projects={str(self.project): {'nth-qweb': remote}})
        with patch('sys.stderr', new_callable=io.StringIO) as stderr:
            command = self.launch().args[0]
        self.assertEqual(command, ['claude', FLAG, 'server:nth-trio'])
        self.assertIn('nth-qweb is also registered in local scope', stderr.getvalue())
        self.assertIn('will NOT be pushed', stderr.getvalue())

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

    def test_an_invocation_that_opens_no_session_reaches_claude_as_typed(self):
        self.configure(quartet_url='http://hub.example/sse')
        # `claude mcp ...` is how a broken registration gets repaired: never preflighted.
        (self.home / '.claude.json').write_text('{not json', encoding='utf-8')
        for arguments in (['mcp', 'list'], ['update'], ['doctor'], ['plugins', 'list'], ['-p', 'summarize this'],
                          ['--model', 'chosen-model', '--print', 'a prompt'], ['--version'], ['-h'],
                          ['--bg', 'a prompt'], ['--', 'mcp', 'list']):
            with self.subTest(arguments=arguments), patch.dict(os.environ, {'TRIO_CLAUDE_CHANNEL': '1',
                                                                            'UNRELATED_SETTING': 'kept'}):
                launched = self.launch(*arguments)
                typed = arguments[1:] if arguments[0] == '--' else arguments
                self.assertEqual(launched.args[0], ['claude', *typed])
                # Its host gives it no channel, so its frontends must not expect one,
                # even when this launcher runs inside a session that has one.
                self.assertNotIn('TRIO_CLAUDE_CHANNEL', launched.kwargs['env'])
                self.assertEqual(launched.kwargs['env']['UNRELATED_SETTING'], 'kept')

    def test_without_a_terminal_nothing_is_added_and_it_says_so(self):
        self.configure()
        self.terminal.stop()
        try:
            with patch.object(nth_cli, 'terminal_attached', return_value=False), \
                 patch('sys.stderr', new_callable=io.StringIO) as stderr:
                self.assertEqual(self.launch('--continue').args[0], ['claude', '--continue'])
                # It looked like a session: passing it through in silence would be deaf by default.
                self.assertIn('without channel delivery', stderr.getvalue())
                said = len(stderr.getvalue())
                # A one-shot never wanted a channel, so there is nothing to say.
                self.launch('-p', 'a prompt')
                self.assertEqual(len(stderr.getvalue()), said)
        finally:
            self.terminal.start()

    def test_a_git_bash_terminal_without_a_pseudo_console_counts_as_a_terminal(self):
        # The pipe names a native Windows program was handed by mintty with MSYS=disable_pcon.
        for name in ('\\msys-1888ae32e00d56aa-pty0-from-master-nat', '\\msys-1888ae32e00d56aa-pty0-to-master-nat',
                     '\\cygwin-e022582115c10879-pty1-from-master'):
            self.assertTrue(nth_cli.MSYS_TERMINAL_PIPE.match(name), name)
        for name in ('\\msys-1888ae32e00d56aa-pipe-0x1', '\\mojo.1234.5678', 'msys-1888ae32e00d56aa-pty0-from-master', ''):
            self.assertFalse(nth_cli.MSYS_TERMINAL_PIPE.match(name), name)
        # An ordinary pipe, as a script or an editor hands over, is not a terminal.
        probe = ('import sys\n'
                 f'sys.path.insert(0, {str(Path(nth_cli.__file__).resolve().parent)!r})\n'
                 'import nth_cli\n'
                 'print(nth_cli.msys_terminal(sys.stdin), nth_cli.terminal_attached())\n')
        done = subprocess.run([sys.executable, '-c', probe], input='', capture_output=True, text=True, timeout=60)
        self.assertEqual(done.stdout.split(), ['False', 'False'], done.stderr[-400:])

    def test_options_before_a_subcommand_do_not_hide_it(self):
        wanted = nth_cli.claude_session_wanted
        for arguments in (['--model', 'chosen-model', 'doctor'], ['--model=chosen-model', 'mcp', 'list'],
                          ['--settings', 'file.json', '--verbose', 'update'], ['-pc', 'a prompt'],
                          ['-cp', 'a prompt']):
            with self.subTest(plain=arguments):
                self.assertFalse(wanted(arguments))
        # Claude Code's own parser gives these words to the option before them, so
        # they are values or a prompt, never a subcommand.
        for arguments in (['--debug', 'mcp', 'list'], ['--add-dir', 'one', 'two', 'mcp'], ['--resume', 'doctor'],
                          ['--model', 'mcp'], ['--continue', 'fix the mcp server'], ['-c'],
                          # A short option with its value attached, not a cluster holding -p.
                          ['-dapi'], ['-rprevious']):
            with self.subTest(session=arguments):
                self.assertTrue(wanted(arguments))

    def test_a_prompt_after_the_separator_is_never_read_as_an_option(self):
        self.configure()
        command = self.launch('--model', 'chosen-model', '--', '-p').args[0]
        # `-p` here is the prompt. The flag sits before the separator, where Claude
        # still reads options, and the separator ends the flag's list.
        self.assertEqual(command, ['claude', '--model', 'chosen-model', FLAG, 'server:nth-trio', '--', '-p'])
        self.assertTrue(nth_cli.claude_session_wanted(['--model', 'chosen-model', '--', 'mcp']))
        self.assertFalse(nth_cli.claude_session_wanted(['mcp', 'list']))

    def test_shell_init_prints_functions_that_call_this_installation_by_path(self):
        with patch.object(nth_cli.sys, 'executable', "/opt/o'brien/python"), \
             patch.object(nth_cli, '__file__', str(self.scripts / 'nth_cli.py')):
            cli = str((self.scripts / 'nth_cli.py').resolve())
            posix = nth_cli.shell_init('bash')
            windows = nth_cli.shell_init('powershell')
            with patch('sys.stdout', new_callable=io.StringIO) as printed:
                nth_cli.main(['shell-init', 'zsh'])
        self.assertEqual(printed.getvalue().strip(), posix)
        self.assertEqual(posix.splitlines()[0],
                         'claude() { ' + shlex.join(["/opt/o'brien/python", cli, 'claude']) + ' "$@"; }')
        self.assertIn('codex() { ', posix.splitlines()[1])
        quoted = "& '/opt/o''brien/python' '" + cli.replace("'", "''") + "' 'claude' @args"
        self.assertEqual(windows.splitlines()[0],
                         'function claude { if ($MyInvocation.ExpectingInput) { $input | ' + quoted
                         + ' } else { ' + quoted + ' } }')
        self.assertTrue(windows.splitlines()[1].startswith('function codex {'))
        # One client only, for a machine where the other is not run through Trio.
        with patch('sys.stdout', new_callable=io.StringIO) as printed:
            nth_cli.main(['shell-init', 'bash', '--clients', 'claude'])
        self.assertEqual([line.split('(')[0] for line in printed.getvalue().splitlines()], ['claude'])

    def test_the_printed_functions_pass_arguments_and_piped_input_through_a_real_shell(self):
        stub = self.home / 'stub_cli.py'
        stub.write_text('import json, sys\n'
                        'print(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read()}))\n')
        awkward = ['-p', 'two words', 'a&b', '$HOME', 'it\'s']
        shells = []
        if os.name != 'nt' and shutil.which('bash'):
            shells.append(('bash', [shutil.which('bash'), '-c'],
                           "printf piped | claude -p 'two words' 'a&b' '$HOME' \"it's\""))
        if shutil.which('pwsh'):
            shells.append(('powershell', [shutil.which('pwsh'), '-NoProfile', '-NonInteractive', '-Command'],
                           "'piped' | claude -p 'two words' 'a&b' '$HOME' 'it''s'"))
        if not shells:
            self.skipTest('no bash (POSIX) or pwsh on this machine')
        for shell, prefix, line in shells:
            with self.subTest(shell=shell), patch.object(nth_cli, '__file__', str(stub)):
                script = nth_cli.shell_init(shell) + '\n' + line
                done = subprocess.run([*prefix, script], capture_output=True, text=True, timeout=60)
                self.assertEqual(done.returncode, 0, done.stderr[-400:])
                seen = json.loads(done.stdout)
                self.assertEqual(seen['argv'], ['claude', *awkward])
                self.assertEqual(seen['stdin'].strip(), 'piped')

    @unittest.skipIf(os.name == 'nt', 'needs POSIX process groups to deliver a terminal interrupt')
    def test_an_interrupt_reaches_the_program_and_never_kills_it_through_the_launcher(self):
        # Ctrl+C in a session cancels a prompt. The terminal sends it to the launcher
        # too, and subprocess.call kills its child when the launcher is interrupted.
        stub = self.home / 'claude-stub'
        stub.write_text('#!' + sys.executable + '\n'
                        'import signal, sys, time\n'
                        'seen = []\n'
                        'signal.signal(signal.SIGINT, lambda *_: seen.append(1))\n'
                        'print("ready", flush=True)\n'
                        'deadline = time.monotonic() + 20\n'
                        'while not seen and time.monotonic() < deadline:\n'
                        '    time.sleep(.05)\n'
                        # Longer than subprocess.call waits before it kills an interrupted child.
                        'time.sleep(1)\n'
                        'print("survived" if seen else "never interrupted", flush=True)\n'
                        'sys.exit(7)\n')
        stub.chmod(0o755)
        self.configure(claude_binary=str(stub))
        cli = Path(__file__).resolve().parents[1] / 'server' / 'nth_cli.py'
        launcher = subprocess.Popen([sys.executable, str(cli), 'claude', '-p', 'a prompt'],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, start_new_session=True)
        try:
            self.assertEqual(launcher.stdout.readline().strip(), 'ready')
            os.killpg(launcher.pid, signal.SIGINT)      # what a terminal does on Ctrl+C
            output, errors = launcher.communicate(timeout=30)
        finally:
            if launcher.poll() is None:
                os.killpg(launcher.pid, signal.SIGKILL)
                launcher.wait()
        self.assertEqual((output.strip(), launcher.returncode), ('survived', 7), errors[-400:])
        self.assertNotIn('KeyboardInterrupt', errors)

    def test_it_never_bypasses_permission_prompts(self):
        self.configure(quartet_url='http://hub.example/sse')
        command = self.launch().args[0]
        self.assertNotIn('--dangerously-skip-permissions', command)
        self.assertEqual([part for part in command if part.startswith('--dangerously')], [FLAG])


if __name__ == '__main__':
    unittest.main()
