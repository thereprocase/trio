"""Opt-in, metadata-only tmux wake sink. Never replay an ambiguous terminal send."""
import json
import re
import subprocess
import threading
import time

from nth_claude_hook import process_stamp
from nth_notice import name

PANE = re.compile(r'%[0-9]{1,10}')
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


def idle_footer(text, client):
    """An empty input, not a prompt quoted in history or text already being typed."""
    lines = [ANSI.sub('', line).strip() for line in text.splitlines()[-6:]]
    marker = '›' if client == 'codex' else '❯'
    # A currently active tool/approval/compact footer always wins over the input.
    busy = re.compile(r'esc to (interrupt|cancel)|thinking|compacting|allow once|allow always|running…', re.I)
    if any(busy.search(line) for line in lines):
        return False
    prompts = [line for line in lines if line.startswith(('❯', '›'))]
    return bool(prompts) and prompts[-1] == marker


def notice(items):
    parts = []
    prefix = 'trio'
    for item in items:
        if item['source'] == 'quartet':
            prefix = 'quartet'
        if item.get('reason'):
            from nth_notice import ended_notice
            parts.append(ended_notice(prefix,item['channel'],item['member_id'],item['reason']))
        elif item.get('count'):
            flag = ' (includes @me)' if item.get('addressed') else ''
            parts.append(f"{int(item['count'])} unread in {name(item['channel'],32)}{flag}")
    return 'Quartet: ' + '; '.join(parts) + f' - poll now with {prefix}_poll, ack with {prefix}_ack.'


def command(*args):
    return subprocess.run(['tmux', *args], capture_output=True, text=True, timeout=2, check=True).stdout


class TmuxSink:
    def __init__(self, store, closing=lambda:False, run=command):
        self.store, self.closing, self.run = store, closing, run
        self.thread = None
        self.last_poll = 0.

    def enqueue(self, session, items):
        """Caller holds writer lock/transaction; cursor and outbox move together."""
        binding = self.store.db.execute('SELECT value FROM meta WHERE key=?',('tmux:'+session,)).fetchone()
        if binding is None:
            return False
        self.store.db.execute('INSERT INTO deliveries(session,sink,body,ranges,state,created) VALUES (?,?,?,?,?,?)',
                              (session,'tmux',notice(items),binding[0],'queued',time.time()))
        return True

    def schedule(self):
        if self.closing() or (self.thread and self.thread.is_alive()) or time.monotonic()-self.last_poll<1:
            return
        self.last_poll = time.monotonic()
        self.thread = threading.Thread(target=self.drain,daemon=True,name='interposer-tmux')
        self.thread.start()

    def drain(self):
        with self.store.lock:
            if self.closing():return
            rows = [dict(r) for r in self.store.db.execute("SELECT * FROM deliveries WHERE sink='tmux' AND state='queued' ORDER BY id LIMIT 32")]
        visited = set()
        for row in rows:
            if row['session'] in visited:continue
            visited.add(row['session'])
            self.deliver(row)

    def deliver(self, row):
        binding = json.loads(row['ranges'])
        pane = binding['pane']
        if not PANE.fullmatch(pane):return
        with self.store.lock:
            if self.closing():return
            state = self.store.session(row['session'])
            current = self.store.db.execute('SELECT value FROM meta WHERE key=?',('tmux:'+row['session'],)).fetchone()
            if state['state']=='ended' or state['sink']!='tmux' or not current or current[0]!=row['ranges']:
                with self.store.db:self.store.db.execute("UPDATE deliveries SET state='cancelled' WHERE id=?",(row['id'],))
                return
        try:
            pid, program = self.run('display-message','-p','-t',pane,'#{pane_pid} #{pane_current_command}').strip().split(' ',1)
            # Pane IDs can be reused after tmux restarts. Bind the exact OS lifetime.
            if int(pid)!=binding['pid'] or process_stamp(int(pid))!=binding['stamp']:return
            if program not in ('claude','codex','opencode','python','python3'):return
            footer = self.run('capture-pane','-p','-t',pane,'-S','-6')
            if not idle_footer(footer,state['client']):return
        except (OSError,ValueError,subprocess.SubprocessError):return
        # Commit an at-most-once claim before any side effect. A crash/timeout after
        # this point requires operator inspection, never blindly typed duplicate text.
        with self.store.lock, self.store.db:
            if self.closing():return
            current = self.store.db.execute('SELECT value FROM meta WHERE key=?',('tmux:'+row['session'],)).fetchone()
            if not current or current[0]!=row['ranges'] or self.store.session(row['session'])['state']=='ended':return
            changed=self.store.db.execute("UPDATE deliveries SET state='sending' WHERE id=? AND state='queued'",(row['id'],)).rowcount
            if not changed:return
        outcome='uncertain'
        try:
            self.run('send-keys','-t',pane,'-l','--',row['body'])
            self.run('send-keys','-t',pane,'Enter')
            outcome='written'
        except (OSError,subprocess.SubprocessError):pass
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE deliveries SET state=?,done=? WHERE id=?',(outcome,time.time(),row['id']))
            self.store.db.execute('UPDATE sessions SET last_wake=?,problem=? WHERE session=?',
                                  (time.time(),'' if outcome=='written' else 'tmux send uncertain; inspect pane',row['session']))
