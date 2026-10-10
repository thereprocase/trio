import sys,json,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
import nth_terminal as t
with tempfile.TemporaryDirectory() as tmp:
 p=Path(tmp)/'cache.json'
 p.write_text(json.dumps({'_cached_rate_limits':{'five_hour':{'used_percentage':24,'resets_at':'2026-10-10T18:00:00Z'},'seven_day':{'used_percentage':51,'resets_at':1800000000}}}))
 u=t.read_usage({'usage_file':str(p),'usage_format':'claude-statusline'})
 assert [r['label'] for r in u['windows']]==['5 hour','Weekly']
 assert u['windows'][0]['updated_at'] is None,'file mtime is not quota freshness'
 assert u['windows'][0]['resets_at']==1791655200
 p.write_text(json.dumps({'timestamp':'2026-10-10T14:00:00Z','payload':{'rate_limits':{'primary':{'used_percent':32,'window_minutes':300,'resets_at':1800000000},'secondary':{'used_percent':78,'window_minutes':10080,'resets_at':1800100000},'credits':{'secret':'omit'}}}})+'\n')
 u=t.read_usage({'usage_file':str(p),'usage_format':'codex-session'})
 assert [r['label'] for r in u['windows']]==['5 hour','Weekly']
 assert 'secret' not in json.dumps(u) and 'credits' not in json.dumps(u)
 assert u['windows'][0]['used_percentage']==32
 assert not t.clean_usage({'windows':[{'used_percentage':float('nan')},{'used_percentage':False},{'used_percentage':101},{'used_percentage':10**1000}]})['windows']
 assert not t.read_usage({})['windows']
print('PASS: Claude/Codex windows, reset times, honest freshness and scalar-only projection')
