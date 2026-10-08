"""Private, content-free shadow evidence and interval comparison. No delivery sink."""
import json
import math
import stat
import os
from pathlib import Path
import statistics
import time

from nth_interposer_wire import home, private_dir, file_lock, IDENTITY_KEY, SESSION_ID, canonical_server
from nth_notice import shown_reason

ROTATE_BYTES = 5 * 1024 * 1024
COMPARE_MARGIN = 3.0


def ranges_for(key, server, messages):
    """Split holes rather than claim filtered or invisible ids were announced."""
    result = []
    server = canonical_server(server)
    for message in messages:
        mid = message['id']
        addressed = bool(message.get('mentioned') or message.get('banged'))
        if result and mid == result[-1]['last'] + 1:
            row = result[-1]
            row['last'], row['count'] = mid, row['count'] + 1
            row['addressed'] |= addressed
        else:
            result.append({'key': key, 'server': server, 'first': mid, 'last': mid,
                           'count': 1, 'addressed': addressed})
    return result


def append(side, session, client, sink, ranges, ended, lines):
    """Project each record anew: no tokens, message text or sender fields escape."""
    if os.environ.get('TRIO_INTERPOSER_SHADOW') == '0':
        return False
    try:
        if side not in ('actual', 'would') or not SESSION_ID.fullmatch(session):
            return False
        if client not in ('claude', 'codex') or sink not in ('rewake', 'queue'):
            return False
        clean_ranges, clean_ended = [], []
        for row in ranges:
            server = canonical_server(row['server'])
            if not IDENTITY_KEY.fullmatch(row['key']):
                return False
            if any(type(row[k]) is not int or not 0 <= row[k] < 2**53 for k in ('first', 'last', 'count')):
                return False
            if row['last'] < row['first']:
                return False
            clean_ranges.append({k: row[k] for k in ('key', 'server', 'first', 'last', 'count')})
            clean_ranges[-1]['server'] = server
            clean_ranges[-1]['addressed'] = bool(row['addressed'])
        for row in ended:
            if not IDENTITY_KEY.fullmatch(row['key']):
                return False
            clean_ended.append({'key': row['key'], 'reason': shown_reason(row['reason'])})
        record = dict(t=time.time(), side=side, session=session, client=client, sink=sink,
                      ranges=clean_ranges, ended=clean_ended, lines=int(lines))
        directory = private_dir(home() / 'events' / 'shadow')
        path = directory / (side + '.jsonl')
        data = (json.dumps(record, separators=(',', ':'), allow_nan=False) + '\n').encode()
        with file_lock(directory / (side + '.lock'), timeout=.02):
            if path.is_symlink():
                return False
            if path.exists() and path.stat().st_size + len(data) > ROTATE_BYTES:
                os.replace(path, path.with_suffix('.jsonl.1'))
            fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            with os.fdopen(fd, 'ab') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    return False
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
        return True
    except Exception:
        return False  # Evidence is best effort and never changes real delivery.


def valid_record(row, side):
    try:
        if (not isinstance(row,dict) or row['side']!=side or not SESSION_ID.fullmatch(row['session'])
                or row['client'] not in ('claude','codex') or row['sink'] not in ('rewake','queue')
                or type(row['t']) not in (int,float) or not math.isfinite(row['t'])
                or type(row['lines']) is not int or row['lines']<0):
            return False
        if not isinstance(row['ranges'],list) or not isinstance(row['ended'],list):
            return False
        for item in row['ranges']:
            canonical_server(item['server'])
            if not IDENTITY_KEY.fullmatch(item['key']):
                return False
            if any(type(item[k]) is not int or not 0<=item[k]<2**53 for k in ('first','last','count')):
                return False
            if item['first']>item['last'] or type(item['addressed']) is not bool:
                return False
        for item in row['ended']:
            if not IDENTITY_KEY.fullmatch(item['key']) or not isinstance(item['reason'],str):
                return False
        return True
    except (KeyError,TypeError,ValueError):
        return False


def records(side, since=None):
    directory = home()/'events'/'shadow'
    cutoff = time.time()-since if since is not None else float('-inf')
    result = []
    for path in (directory/(side+'.jsonl.1'),directory/(side+'.jsonl')):
        try:
            fd = os.open(path,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW)
            with os.fdopen(fd,'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    continue
                data = stream.read(5*1024*1024+1)
            for line in data.splitlines():
                try:
                    row = json.loads(line)
                    if valid_record(row,side) and row['t']>=cutoff:
                        result.append(row)
                except (ValueError,UnicodeError,RecursionError):
                    continue
        except OSError:
            continue
    return result


def merge(intervals):
    result = []
    for first, last in sorted(intervals):
        if result and first <= result[-1][1] + 1:
            result[-1][1] = max(result[-1][1], last)
        else:
            result.append([first, last])
    return result


def subtract(left, right):
    """Bound memory by ranges, even for a 50,000-message flood."""
    result = []
    for first, last in merge(left):
        cursor = first
        for start, end in merge(right):
            if end < cursor:
                continue
            if start > last:
                break
            if start > cursor:
                result.append([cursor, min(last, start - 1)])
            cursor = max(cursor, end + 1)
        if cursor <= last:
            result.append([cursor, last])
    return result


def compare(since=None):
    logs = {side:records(side) for side in ('actual','would')}
    missing = {'missing_in_would':[],'missing_in_actual':[]}
    if not all(logs.values()):
        return {**missing,'sessions':{},'median_release_delay':None,'window':None,'comparable':False}
    # Match against all retained evidence. Only reporting is windowed: settling
    # can place a counterpart just beyond a boundary, including --since.
    lower = max(min(r['t'] for r in rows) for rows in logs.values())+COMPARE_MARGIN
    upper = min(max(r['t'] for r in rows) for rows in logs.values())-COMPARE_MARGIN
    if since is not None:
        lower = max(lower,time.time()-since)
    counts,ids,timed,owners = {},{'actual':{},'would':{}},{'actual':{},'would':{}},{}
    retained = {'actual':{},'would':{}}
    for side,rows in logs.items():
        for row in sorted(rows,key=lambda r:r['t']):
            for item in row['ranges']:
                retained[side].setdefault(item['key'],[]).append((item['first'],item['last']))
                timed[side].setdefault(item['key'],[]).append((item['first'],item['last'],row['t']))
            if not lower<=row['t']<=upper:
                continue
            count = counts.setdefault(row['session'],{'actual_notices':0,'would_notices':0})
            count[side+'_notices'] += 1
            for item in row['ranges']:
                key = item['key']
                span = item['first'],item['last']
                ids[side].setdefault(key,[]).append(span)
                if side=='would' or key not in owners:
                    owners[key] = row['session']
    delays = []
    # Membership identity is the mailbox; holder session changes are not missing IDs.
    for key in sorted(ids['actual'].keys()|ids['would'].keys()):
        for side,other in (('actual','would'),('would','actual')):
            for first,last in subtract(ids[side].get(key,[]),retained[other].get(key,[])):
                missing['missing_in_'+other].append(dict(session=owners[key],key=key,first=first,last=last))
        for first,last,actual in timed['actual'].get(key,[]):
            matched = [would for start,end,would in timed['would'].get(key,[])
                       if max(first,start)<=min(last,end) and
                       (lower<=actual<=upper or lower<=would<=upper)]
            if matched:
                delays.append(min(matched)-actual)
    return {**missing,'sessions':counts,'median_release_delay':statistics.median(delays) if delays else None,
            'window':{'from':lower,'through':upper,'margin':COMPARE_MARGIN},'comparable':lower<=upper}
