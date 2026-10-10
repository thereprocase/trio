"""Agent-only wake groups behave identically for web and MCP senders."""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory(prefix='nth-bang-agents-') as home:
    os.environ['NTH_HOME'] = home
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
    import nth_server
    import nth_web
    for legacy in (False, True):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE members (id TEXT, name TEXT, channel TEXT' + ('' if legacy else ', kind TEXT') + ')')
        rows = [('a', 'Worker', 'test', 'agent'), ('_op_h', 'Human', 'test', 'human'),
                ('_op_reserved', 'agents', 'test', 'human'), ('b', 'Helper', 'test', 'agent'),
                ('outside', 'Outside', 'other', 'agent')]
        for row in rows:
            db.execute('INSERT INTO members VALUES (' + ','.join('?' * (3 if legacy else 4)) + ')', row[:3] if legacy else row)
        if not legacy:
            db.execute("INSERT INTO members VALUES ('guest', 'Visitor', 'test', 'human')")
        everyone = {r['id'] for r in db.execute("SELECT id FROM members WHERE channel='test'")}
        for parse in (nth_server._parse_sigils, nth_web._parse_sigils_against_roster):
            for content, expected in [('!agents', {'a', 'b'}), ('!AGENTS, please', {'a', 'b'}),
                    ('!agents !Worker !agents', {'a', 'b'}), ('!agents !Human', {'a', 'b', '_op_h'}),
                    ('!agents-helper', set()), ('!agents2', set()), ('@agents #agents', set()),
                    ('!all !agents', everyone)]:
                bangs = parse(db, 'test', content)[2]
                assert set(bangs) == expected, (parse.__name__, legacy, content, bangs)
                assert len(bangs) == len(set(bangs)), bangs
        db.close()
print('PASS: web/MCP agent groups, humans, legacy operators, channel scope, deduplication and token boundaries')
