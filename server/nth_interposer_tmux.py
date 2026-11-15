"""Opt-in, metadata-only tmux wake sink. Never replay an ambiguous terminal send."""
import json
import re
import subprocess
import threading
import time

from nth_claude_hook import process_stamp
from nth_interposer_wire import WireError
from nth_notice import batch_notice_lines

PANE = re.compile(r'%[0-9]{1,10}')
# CSI sequences and OSC strings (tmux emits OSC 8 hyperlinks in -e captures).
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)')
CONTROL = re.compile(r'[\x00-\x1f\x7f]+')

# Every tmux call is bounded so a hung server cannot hold the store lock (the send
# fence below holds it across three calls) for longer than a few seconds.
TMUX_TIMEOUT_S = 2
# Minimum spacing between drain threads; Runtime.tick() calls schedule() far more often.
POLL_INTERVAL_S = 1
# Sessions attempted per drain. Each session gets at most one row per drain.
SESSIONS_PER_DRAIN = 32
# Lines captured from the bottom of the pane: enough for a spinner, a short todo
# list under it and the input box with its status line.
CAPTURE_LINES = 24
# The input prompt must sit this close to the bottom of the capture, or the marker
# found is history rather than the live input box.
FOOTER_LINES = 6
# Programs that host a supported client in the bound pane.
CLIENT_PROGRAMS = ('claude', 'codex', 'opencode', 'python', 'python3')

# Codex has drawn its composer marker as U+203A and, in current builds, U+00BB.
CODEX_MARKERS = ('›', '»')
PROMPT_MARKERS = ('❯',) + CODEX_MARKERS
# Claude Code draws a horizontal rule directly under the input line; any other line
# there is a continuation of a multi-line input.
RULE = re.compile(r'^[─━-]{8,}$')
# Codex prints only these hints under its composer; anything else may be input.
CODEX_HINT = re.compile(r'context left|for shortcuts|⏎ send|newline|transcript|quit|^ctx \d+%', re.I)
# Shown under an empty Codex composer and removed as soon as a draft exists.
CODEX_EMPTY_HINT = 'for shortcuts'
# The live activity line: a spinner glyph at the left edge, a status word and a
# timer/interrupt parenthetical. Claude draws it at column 0 and indents all answer
# text, so a finished answer that mentions "thinking" or quotes a spinner never matches.
CLAUDE_ACTIVE = re.compile(r'^[✢✳✶✻✽·*]\s+\S[^(]*…\s*\([^)]*(?:\d+[smh]\b|esc to interrupt|tokens)')
# Codex draws "• Working (12s • esc to interrupt)"; its answers start with "• " too,
# so the timer and interrupt parenthetical are both required.
CODEX_ACTIVE = re.compile(r'^ ?(?:[•◦·]\s+)?\S[^(]*\(\d+[smh][^)]*esc to interrupt\)')


def _screen(text):
    """Split a capture into (plain, solid) line pairs for the bottom of the pane.

    plain is the visible text; solid is the same text without faint (SGR 2)
    characters. Both clients draw placeholder text faint and typed input at normal
    weight, so solid tells an empty input box from a draft. SGR state carries across
    lines, as tmux emits it."""
    pairs, faint = [], False
    for raw in text.splitlines():
        plain, solid, at = [], [], 0
        for found in ANSI.finditer(raw):
            chunk = raw[at:found.start()]
            plain.append(chunk)
            solid.append('' if faint else chunk)
            at = found.end()
            if found.group().startswith('\x1b[') and found.group().endswith('m'):
                faint = _faint(found.group()[2:-1], faint)
        chunk = raw[at:]
        plain.append(chunk)
        solid.append('' if faint else chunk)
        pairs.append(tuple(''.join(part).replace('\xa0', ' ').rstrip() for part in (plain, solid)))
    while pairs and not pairs[-1][0].strip():
        pairs.pop()
    return pairs[-CAPTURE_LINES:]


def _faint(params, faint):
    """Apply one SGR parameter list to the faint flag."""
    codes = params.split(';') if params else ['0']
    i = 0
    while i < len(codes):
        code = codes[i]
        if code in ('38', '48', '58'):
            # Extended colour: "5;n" or "2;r;g;b" follow and are not attributes.
            i += {'5': 2, '2': 4}.get(codes[i+1] if i+1 < len(codes) else '', 0)
        elif code in ('', '0', '22'):
            faint = False
        elif code == '2':
            faint = True
        i += 1
    return faint


def idle_footer(text, client):
    """True only for an affirmatively empty input box with no live activity above it.

    Anything ambiguous returns False: delivery waits for a later poll rather than
    typing into a busy client or submitting the user's unfinished prompt."""
    screen = _screen(text)
    lines = [plain for plain, _ in screen]
    markers = CODEX_MARKERS if client == 'codex' else ('❯',)
    at = next((i for i in range(len(lines)-1, -1, -1) if lines[i].lstrip().startswith(PROMPT_MARKERS)), None)
    if at is None or at < len(lines)-FOOTER_LINES or not lines[at].strip().startswith(markers):
        return False
    # Faint placeholder text after the marker is an empty box; anything solid is a draft.
    typed = screen[at][1].strip()
    if typed.startswith(markers):
        typed = typed[1:]
    if typed.strip():
        return False
    below = [line.strip() for line in lines[at+1:]]
    if client == 'codex':
        if not _codex_footer(below):
            return False
        active = CODEX_ACTIVE
    else:
        if not below or not RULE.match(below[0]):
            return False
        active = CLAUDE_ACTIVE
    return not any(active.match(line) for line in lines[:at])


def _codex_footer(below):
    """True when the lines under the Codex composer are footer, not continued input.

    Older builds print hint lines only. Current builds print a blank line, a status
    line (model and directory) and a hint line that carries CODEX_EMPTY_HINT only
    while the composer is empty."""
    other = [i for i, line in enumerate(below) if line and not CODEX_HINT.search(line)]
    if not other:
        return True
    return (len(other) == 1 and other[0] > 0 and not below[0]
            and other[0]+1 < len(below) and CODEX_EMPTY_HINT in below[other[0]+1])


def notice(items):
    """The wake text typed into a pane: the shared notice lines joined on one line,
    since a newline in the pane would submit a partial notice."""
    return CONTROL.sub(' ', ' '.join(batch_notice_lines(items)))


def command(*args):
    return subprocess.run(['tmux', *args], capture_output=True, text=True, timeout=TMUX_TIMEOUT_S, check=True).stdout


class TmuxSink:
    def __init__(self, store, closing=lambda:False, run=command):
        self.store, self.closing, self.run = store, closing, run
        self.thread = None
        self.last_poll = 0.
        # Last session attempted; the next drain starts after it so that more than
        # SESSIONS_PER_DRAIN pending sessions are all reached in turn.
        self.after = ''

    def enqueue(self, session, items):
        """Caller holds writer lock/transaction; cursor and outbox move together."""
        binding = self.store.db.execute('SELECT value FROM meta WHERE key=?',('tmux:'+session,)).fetchone()
        if binding is None:
            return False
        self.store.db.execute('INSERT INTO deliveries(session,sink,body,ranges,state,created) VALUES (?,?,?,?,?,?)',
                              (session,'tmux',notice(items),binding[0],'queued',time.time()))
        return True

    def schedule(self):
        if self.closing() or (self.thread and self.thread.is_alive()) or time.monotonic()-self.last_poll<POLL_INTERVAL_S:
            return
        self.last_poll = time.monotonic()
        self.thread = threading.Thread(target=self.drain,daemon=True,name='interposer-tmux')
        self.thread.start()

    def drain(self):
        with self.store.lock:
            if self.closing():return
            # The oldest queued row of each session (FIFO within a session), so a busy
            # pane with many queued rows occupies one slot, never the whole batch.
            heads = [dict(r) for r in self.store.db.execute(
                "SELECT d.* FROM deliveries d WHERE d.sink='tmux' AND d.state='queued' AND d.id="
                "(SELECT MIN(id) FROM deliveries WHERE session=d.session AND sink='tmux' AND state='queued')"
                " ORDER BY d.session")]
        heads = [r for r in heads if r['session']>self.after]+[r for r in heads if r['session']<=self.after]
        for row in heads[:SESSIONS_PER_DRAIN]:
            self.after = row['session']
            try:
                self.deliver(row)
            except WireError:
                # The session was evicted; its row can never be delivered, and it must
                # not stop delivery to the other sessions.
                self.cancel(row)

    def cancel(self, row):
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE deliveries SET state='cancelled',done=? WHERE id=? AND state='queued'",(time.time(),row['id']))

    def admissible(self, row):
        """Whether the row may be typed now. Caller holds the store lock.

        'send': session, sink and binding still match the row. 'rebound': the session
        was bound to another pane since the row was read; the row is moved to the new
        binding and the next drain inspects that pane. 'cancel': the session ended or
        left the tmux sink. Raises WireError when the session no longer exists."""
        state = self.store.session(row['session'])
        current = self.store.db.execute('SELECT value FROM meta WHERE key=?',('tmux:'+row['session'],)).fetchone()
        if state['state']=='ended' or state['sink']!='tmux' or current is None:
            self.cancel(row)
            return 'cancel', state
        if current[0]!=row['ranges']:
            with self.store.db:
                self.store.db.execute("UPDATE deliveries SET ranges=? WHERE id=? AND state='queued'",(current[0],row['id']))
            return 'rebound', state
        return 'send', state

    def identity(self, pane, binding):
        """The pane still runs the bound host process lifetime and a client program."""
        pid, program = self.run('display-message','-p','-t',pane,'#{pane_pid} #{pane_current_command}').strip().split(' ',1)
        # Pane IDs can be reused after tmux restarts. Bind the exact OS lifetime.
        if int(pid)!=binding['pid'] or process_stamp(int(pid))!=binding['stamp'] or program not in CLIENT_PROGRAMS:
            return False
        # A pane's shell outlives a client that crashed without ending its session;
        # the bound client must itself be alive, or a new client in the pane would
        # receive the old session's notices.
        client = binding.get('client')
        return client is None or process_stamp(client)==binding.get('client_stamp')

    def deliver(self, row):
        try:
            binding = json.loads(row['ranges'])
            pane = binding['pane']
        except (TypeError,ValueError,KeyError):
            return self.cancel(row)
        if not isinstance(pane,str) or not PANE.fullmatch(pane):
            return self.cancel(row)
        with self.store.lock:
            if self.closing():return
            verdict, state = self.admissible(row)
            if verdict!='send':return
        # Unlocked inspection: a busy pane is the common case and must not hold the lock.
        try:
            if not self.identity(pane,binding):return
            footer = self.run('capture-pane','-p','-e','-t',pane,'-S',f'-{CAPTURE_LINES}')
            if not idle_footer(footer,state['client']):return
        except (OSError,ValueError,subprocess.SubprocessError):return
        # The fence: register/end take the store lock, so holding it from the final
        # binding check through both sends means a rebind or end lands either before
        # the check (row cancelled) or after the text reached the bound pane.
        # Hold time is bounded by three TMUX_TIMEOUT_S calls.
        with self.store.lock:
            if self.closing():return
            verdict, _ = self.admissible(row)
            if verdict!='send':return
            try:
                if not self.identity(pane,binding):return
            except (OSError,ValueError,subprocess.SubprocessError):return
            # Commit an at-most-once claim before any side effect. A crash/timeout after
            # this point requires operator inspection, never blindly typed duplicate text.
            with self.store.db:
                changed=self.store.db.execute("UPDATE deliveries SET state='sending' WHERE id=? AND state='queued'",(row['id'],)).rowcount
            if not changed:return
            outcome='uncertain'
            try:
                self.run('send-keys','-t',pane,'-l','--',row['body'])
                self.run('send-keys','-t',pane,'Enter')
                outcome='written'
            except (OSError,subprocess.SubprocessError):pass
            with self.store.db:
                self.store.db.execute('UPDATE deliveries SET state=?,done=? WHERE id=?',(outcome,time.time(),row['id']))
                self.store.db.execute('UPDATE sessions SET last_wake=?,problem=? WHERE session=?',
                                      (time.time(),'' if outcome=='written' else 'tmux send uncertain; inspect pane',row['session']))
