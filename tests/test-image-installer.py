"""Installer admission must detect a missing or incompatible image decoder."""
import contextlib,io,pathlib,re,sys,types
from unittest.mock import patch
source=(pathlib.Path(__file__).resolve().parents[1]/'setup.sh').read_text()
checks=re.findall(r'-c "([^"\n]*from mcp.server.fastmcp[^"\n]*)"',source)
# The two hub/spoke guards are inline; the hub-service guard became HUB_PIN_CHECK
# (distribution versions plus imports) and is exercised separately below.
assert len(checks)==2
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
pin=re.search(r"HUB_PIN_CHECK='(.*?)'\n",source,re.S).group(1)
def pin_check(pillow):
    versions={'mcp':'1.9.0','uvicorn':'0.52.1','Pillow':pillow}
    def version(name):
        import importlib.metadata as md
        if versions.get(name) is None: raise md.PackageNotFoundError(name)
        return versions[name]
    modules['PIL'].__version__=pillow or '0'
    with patch.dict(sys.modules,modules),patch('importlib.metadata.version',version):
        try:
            with contextlib.redirect_stdout(io.StringIO()): exec(pin,{'__name__':'pin'})
        except SystemExit as e: return e.code==0
for version,accepted in [('12.2.0',False),('12.3.0',True),('13.0.0',False),(None,False)]:
    assert pin_check(version)==accepted,version
print('PASS: all four installer guards reject absent/old/new-major Pillow and accept12.3')
