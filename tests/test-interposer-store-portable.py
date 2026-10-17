"""The installer opens the interposer trust store on every platform.

setup.py records the operator's hubs in the store on Windows too, where os.getuid
does not exist. Opening and closing a fresh store must work on the current platform,
and on Windows a junction must not pass for a private directory.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))

with tempfile.TemporaryDirectory(prefix='nth-store-portable-') as tmp:
    os.environ['NTH_HOME'] = tmp
    from nth_interposer_store import Store
    import nth_interposer_wire as wire
    store = Store(Path(tmp) / 'events' / 'interposer.sqlite')
    store.close()
    assert (Path(tmp) / 'events').is_dir(), 'store directory was not created'
    print(f'PASS: interposer store opens on {sys.platform}')

    if os.name == 'nt':
        # mklink /J needs no privilege, which is exactly why a junction is the threat.
        target = Path(tmp) / 'elsewhere'
        target.mkdir()
        junction = Path(tmp) / 'run'
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(target)],
                       check=True, capture_output=True)
        try:
            wire.private_dir(junction)
        except PermissionError:
            print('PASS: a junction is refused as a private directory')
        else:
            raise AssertionError('private_dir accepted a junction')
        finally:
            os.rmdir(junction)                   # removes the junction, not its target

        # The Claude hook's state directory decides what a session listens to.
        import nth_claude_hook
        hooks = Path(tmp) / 'events' / 'hooks'
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(hooks), str(target)],
                       check=True, capture_output=True)
        try:
            nth_claude_hook.hooks_dir()
        except PermissionError:
            print('PASS: a junction is refused as the hook state directory')
        else:
            raise AssertionError('hooks_dir accepted a junction')
        finally:
            os.rmdir(hooks)
