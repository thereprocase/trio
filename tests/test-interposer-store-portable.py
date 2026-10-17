"""The installer opens the interposer trust store on every platform.

setup.py records the operator's hubs in the store on Windows too, where os.getuid
does not exist. Opening and closing a fresh store must work on the current platform.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))

with tempfile.TemporaryDirectory(prefix='nth-store-portable-') as tmp:
    os.environ['NTH_HOME'] = tmp
    from nth_interposer_store import Store
    store = Store(Path(tmp) / 'events' / 'interposer.sqlite')
    store.close()
    assert (Path(tmp) / 'events').is_dir(), 'store directory was not created'
    print(f'PASS: interposer store opens on {sys.platform}')
