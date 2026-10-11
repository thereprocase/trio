"""Installer admission must detect a missing or incompatible image decoder."""
import pathlib,re,sys,types
from unittest.mock import patch
source=(pathlib.Path(__file__).resolve().parents[1]/'setup.sh').read_text()
checks=re.findall(r'-c "([^"\n]*from mcp.server.fastmcp[^"\n]*)"',source)
assert len(checks)==4
modules={name:types.ModuleType(name) for name in ['mcp','mcp.server','mcp.server.fastmcp','uvicorn','PIL']}
modules['mcp.server.fastmcp'].FastMCP=object
for check in checks:
    assert 'import PIL' in check
    for version,accepted in [('12.2.0',False),('12.3.0',True),('13.0.0',False)]:
        modules['PIL'].__version__=version
        with patch.dict(sys.modules,modules):
            try: exec(check,{})
            except AssertionError: assert not accepted
            else: assert accepted
    missing=dict(modules);missing['PIL']=None
    with patch.dict(sys.modules,missing):
        try: exec(check,{})
        except ImportError: pass
        else: raise AssertionError('missing Pillow accepted')
print('PASS: all four installer guards reject absent/old/new-major Pillow and accept12.3')
