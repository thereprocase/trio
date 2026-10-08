// Message times: seconds everywhere, a Local / UTC preference, and the exact
// UTC instant (ISO 8601, milliseconds, Z) on every <time datetime> — because
// operators line chat messages up against bus traces and logs, where a
// minute-resolution "10:25 AM" cannot tell which of forty frames it meant.
//
// The fixture zone is pinned so local-time assertions are deterministic and so
// local and UTC midnights fall in DIFFERENT places (UTC-4 in October).
//
// Usage: node tests/test-message-times.js
'use strict';

process.env.TZ = 'America/New_York';

const assert = require('assert');
const { load } = require('./dom-harness');

const cx = load();
const H = cx.hooks;
const Trio = H.Trio;
const document = cx.document;
const T = Trio.time;

const failures = [];
let passed = 0;
async function check(name, fn) {
  try { await fn(); passed++; console.log('PASS: ' + name); }
  catch (e) { failures.push(name); console.log('FAIL: ' + name + ' — ' + e.message); }
}

// What the hub actually writes: Python datetime.now(timezone.utc).isoformat().
const PY_ISO = '2026-10-08T14:25:12.262123+00:00';
const tick = () => new Promise(resolve => setTimeout(resolve, 5));

function setMode(mode) { Trio.preferences.save({ messageTimes: mode }); }
function msg(id, created_at, extra = {}) {
  return { id, member_id: 'ag_a', member_name: 'Ada', channel: 'test', content: 'm' + id, created_at,
    mentions: [], refs: [], bangs: [], recipients: [], ...extra };
}
function stampOf(card) { return card.querySelector('time'); }
function fakeEvent(type, target, extra = {}) {
  const ev = { type, target, prevented: false, stopped: false, ...extra };
  ev.preventDefault = () => { ev.prevented = true; };
  ev.stopPropagation = () => { ev.stopped = true; };
  return ev;
}
// Capture the user-visible side effects of a copy.
function stubUi({ fail = false } = {}) {
  const seen = { copied: [], toasts: [] };
  Trio.ui.copyText = text => { seen.copied.push(text); return fail ? Promise.reject(new Error('denied')) : Promise.resolve(); };
  Trio.ui.toast = text => { seen.toasts.push(text); };
  return seen;
}

(async () => {
  // ── parsing and formatting ──────────────────────────────────────────────
  await check('microsecond Python isoformat parses to the exact millisecond instant', () => {
    assert.strictEqual(T.iso(PY_ISO), '2026-10-08T14:25:12.262Z');
  });
  await check('local clock is 24-hour HH:MM:SS with seconds', () => {
    assert.strictEqual(T.clock(PY_ISO, 'local'), '10:25:12');
    assert.strictEqual(T.clock('2026-10-08T01:02:03.000+00:00', 'local'), '21:02:03');
  });
  await check('UTC clock is HH:MM:SSZ', () => {
    assert.strictEqual(T.clock(PY_ISO, 'utc'), '14:25:12Z');
    assert.strictEqual(T.clock('2026-10-08T01:02:03+00:00', 'utc'), '01:02:03Z');
  });
  await check('other ISO shapes normalise to the same instant', () => {
    assert.strictEqual(T.iso('2026-10-08T14:25:12Z'), '2026-10-08T14:25:12.000Z');
    assert.strictEqual(T.iso('2026-10-08T14:25:12.2Z'), '2026-10-08T14:25:12.200Z');
    assert.strictEqual(T.iso('2026-10-08 14:25:12'), '2026-10-08T14:25:12.000Z', 'zoneless is read as UTC');
    assert.strictEqual(T.iso('2026-10-08T19:55:12.262123+0530'), '2026-10-08T14:25:12.262Z');
    assert.strictEqual(T.iso('2026-10-08T10:25:12.262-04:00'), '2026-10-08T14:25:12.262Z');
  });
  await check('malformed input shows the raw string, never "Invalid Date"', () => {
    for (const bad of ['yesterday-ish', 'now', '2026-13-45T99:99:99Z', 'Invalid Date?', '2026-10-08T']) {
      const out = T.clock(bad, 'local');
      assert.strictEqual(out, bad);
      assert.ok(!/Invalid Date|NaN/.test(T.clock(bad, 'utc').replace(bad, '')));
      assert.strictEqual(T.iso(bad), '');
      assert.strictEqual(T.day(bad), '');
    }
    assert.strictEqual(T.clock(null), '');
    assert.strictEqual(T.clock(''), '');
  });

  // ── the conversation stamp ──────────────────────────────────────────────
  await check('default preference is Local', () => {
    Trio.preferences.reset();
    assert.strictEqual(Trio.preferences.read().messageTimes, 'local');
    assert.strictEqual(T.mode(), 'local');
  });
  await check('message stamp is a <time> with the ISO instant in datetime and the tooltip', () => {
    const time = stampOf(H.cardFor(msg(1, PY_ISO)));
    assert.ok(time, 'a <time> element is rendered');
    assert.strictEqual(time.getAttribute('datetime'), '2026-10-08T14:25:12.262Z');
    assert.strictEqual(time.getAttribute('title'), '2026-10-08T14:25:12.262Z');
    assert.strictEqual(time.getAttribute('tabindex'), '0', 'keyboard users can reach it');
    assert.ok(time.classList.contains('msg-time'));
    assert.ok(time.textContent.endsWith('10:25:12'), time.textContent);
    assert.ok(time.textContent.startsWith('#1 · '), 'the message-number prefix is kept');
  });
  await check('with the preference on UTC, stamps read HH:MM:SSZ', () => {
    setMode('utc');
    assert.strictEqual(T.mode(), 'utc');
    const time = stampOf(H.cardFor(msg(2, PY_ISO)));
    assert.ok(time.textContent.endsWith('14:25:12Z'), time.textContent);
    assert.strictEqual(time.getAttribute('datetime'), '2026-10-08T14:25:12.262Z', 'the instant does not depend on the mode');
    setMode('local');
  });
  await check('malformed created_at renders the raw string with no datetime attribute', () => {
    const card = H.cardFor(msg(3, 'not-a-time'));
    const time = stampOf(card);
    assert.ok(time.textContent.endsWith('not-a-time'));
    assert.strictEqual(time.getAttribute('datetime'), null);
    assert.ok(!card.textContent.includes('Invalid Date'));
  });
  await check('edited marker tooltip carries the seconds and the ISO instant', () => {
    const card = H.cardFor(msg(4, PY_ISO, { edited_at: '2026-10-08T14:30:00.5+00:00' }));
    const mark = card.querySelector('.edited-mark');
    assert.strictEqual(mark.title, 'edited 10:30:00 (2026-10-08T14:30:00.500Z)');
  });

  // ── preference persistence and settings UI ──────────────────────────────
  await check('the preference persists to storage and survives a reload', () => {
    setMode('utc');
    const stored = JSON.parse(cx.window.localStorage.getItem('trio.preferences.v1'));
    assert.strictEqual(stored.messageTimes, 'utc');
    Trio.preferences.apply();   // re-read from storage, as a page load does
    assert.strictEqual(Trio.preferences.read().messageTimes, 'utc');
  });
  await check('an unknown stored value falls back to Local', () => {
    cx.window.localStorage.setItem('trio.preferences.v1', JSON.stringify({ messageTimes: 'zulu' }));
    Trio.preferences.apply();
    assert.strictEqual(Trio.preferences.read().messageTimes, 'local');
  });
  await check('reset returns Message times to Local', () => {
    setMode('utc');
    Trio.preferences.reset();
    assert.strictEqual(Trio.preferences.read().messageTimes, 'local');
  });
  await check('Settings has a "Message times" select offering Local and UTC that saves the choice', () => {
    const panel = document.createElement('div');
    Trio.preferences.renderPage(panel);
    const select = panel.querySelectorAll('select').find(s => s.getAttribute('aria-label') === 'Message times');
    assert.ok(select, 'select is rendered');
    const options = select.querySelectorAll('option').map(o => [o.value, o.textContent]);
    assert.deepStrictEqual(options, [['local', 'Local'], ['utc', 'UTC']]);
    select.value = 'utc';
    select._listeners.change.forEach(fn => fn({ target: select }));
    assert.strictEqual(Trio.preferences.read().messageTimes, 'utc');
    Trio.preferences.reset();
  });

  // ── day separators around midnight ──────────────────────────────────────
  // A 23:30 and B 00:30 local (New York) are the same UTC day; C 23:59:59.999
  // and D 00:00:01 UTC are the same local day. Each mode must split exactly
  // the pair that crosses ITS midnight.
  const A = msg(10, '2026-10-08T03:30:00.000000+00:00');
  const B = msg(11, '2026-10-08T04:30:00.000000+00:00');
  const C = msg(12, '2026-10-08T23:59:59.999000+00:00');
  const D = msg(13, '2026-10-09T00:00:01.000000+00:00');
  function layout() {
    return document.getElementById('messages').children
      .filter(el => el.classList && (el.classList.contains('day-separator') || el.dataset?.messageId))
      .map(el => el.classList.contains('day-separator') ? '|' : el.dataset.messageId).join(' ');
  }
  function seedAll() {
    Trio.state.channel = 'test'; Trio.state.dmKey = '';
    Trio.state.messages = new Map([A, B, C, D].map(m => [m.id, m]));
    Trio.state.messageDomById = new Map();
    Trio.state.lastSeenByConv = {}; Trio.state.dividerBaseByConv = {};
  }
  await check('local mode: separators split at local midnight', () => {
    // In the page, save() fires preferences:changed and the conversation
    // re-renders; the harness does not mount features, so render directly.
    seedAll(); setMode('local'); H.render();
    assert.strictEqual(layout(), '| 10 | 11 12 13');
  });
  await check('UTC mode: separators split at UTC midnight and are labelled UTC', () => {
    seedAll(); setMode('utc'); H.render();
    assert.strictEqual(layout(), '| 10 11 12 | 13');
    const labels = document.getElementById('messages').children.filter(el => el.classList?.contains('day-separator')).map(el => el.textContent);
    assert.ok(labels.every(l => l.endsWith(' UTC')), labels.join(', '));
    setMode('local');
  });
  await check('live messages crossing midnight get a separator without a full re-render', async () => {
    setMode('local');
    Trio.state.channel = 'test'; Trio.state.dmKey = '';
    Trio.state.messages = new Map(); Trio.state.messageDomById = new Map();
    H.render();
    [A, B, C, D].forEach(m => H.upsert({ ...m }));
    await tick();   // the prime batch flushes on the next frame
    assert.strictEqual(layout(), '| 10 | 11 12 13');
    // And an older message arriving late keeps the boundaries right.
    H.upsert(msg(9, '2026-10-07T12:00:00.000000+00:00'));
    await tick();
    assert.strictEqual(layout(), '| 9 10 | 11 12 13');
  });

  // ── copy on tap ─────────────────────────────────────────────────────────
  await check('tapping a time copies the ISO instant and confirms "Copied"', async () => {
    const seen = stubUi();
    const time = stampOf(H.cardFor(msg(20, PY_ISO)));
    const ev = fakeEvent('click', time);
    document.dispatchEvent(ev);
    await tick();
    assert.deepStrictEqual(seen.copied, ['2026-10-08T14:25:12.262Z']);
    assert.ok(seen.toasts[0].startsWith('Copied 2026-10-08T14:25:12.262Z'), seen.toasts[0]);
    assert.ok(ev.stopped, 'the tap does not also open the card underneath');
    assert.ok(time.classList.contains('copied'));
  });
  await check('keyboard Enter on a focused time copies it', async () => {
    const seen = stubUi();
    const time = stampOf(H.cardFor(msg(21, PY_ISO)));
    document.dispatchEvent(fakeEvent('keydown', time, { key: 'Tab' }));
    document.dispatchEvent(fakeEvent('keydown', time, { key: 'Enter' }));
    await tick();
    assert.deepStrictEqual(seen.copied, ['2026-10-08T14:25:12.262Z']);
  });
  // Clipboard writes need user activation. A timer callback has none, so a
  // long press only ARMS the copy and the release (pointerup / touchend, both
  // activating gestures) performs it. "Synchronously inside the up dispatch"
  // is what the assertions below pin: copied is checked before any await.
  const hold = () => new Promise(resolve => setTimeout(resolve, 560));   // past the long-press threshold
  await check('touch long-press: the timer only arms; the copy runs inside the pointerup handler', async () => {
    const seen = stubUi();
    const time = stampOf(H.cardFor(msg(26, PY_ISO)));
    document.dispatchEvent(fakeEvent('pointerdown', time, { pointerType: 'touch', clientX: 5, clientY: 5 }));
    await hold();
    assert.deepStrictEqual(seen.copied, [], 'nothing is copied from the timer');
    const up = fakeEvent('pointerup', time, { pointerType: 'touch' });
    document.dispatchEvent(up);
    assert.deepStrictEqual(seen.copied, ['2026-10-08T14:25:12.262Z'], 'copied during the pointerup dispatch');
    document.dispatchEvent(fakeEvent('touchend', time));
    assert.strictEqual(seen.copied.length, 1, 'touchend for the same gesture does not copy again');
    await new Promise(resolve => setTimeout(resolve, 900));   // past the repeat guard
    const late = fakeEvent('click', time);
    document.dispatchEvent(late);
    await tick();
    assert.strictEqual(seen.copied.length, 1, 'the click after the release is swallowed');
    assert.ok(late.stopped, 'the late click still does not reach the card');
  });
  await check('touch long-press: a browser pointercancel keeps the arm and touchend copies', async () => {
    const seen = stubUi();
    const time = stampOf(H.cardFor(msg(27, PY_ISO)));
    document.dispatchEvent(fakeEvent('pointerdown', time, { pointerType: 'touch', clientX: 5, clientY: 5 }));
    await hold();
    document.dispatchEvent(fakeEvent('pointercancel', time));
    assert.deepStrictEqual(seen.copied, []);
    document.dispatchEvent(fakeEvent('touchend', time));
    assert.deepStrictEqual(seen.copied, ['2026-10-08T14:25:12.262Z']);
  });
  await check('a quick touch tap copies through its click, and a scroll disarms a hold', async () => {
    const seen = stubUi();
    const tap = stampOf(H.cardFor(msg(28, PY_ISO)));
    document.dispatchEvent(fakeEvent('pointerdown', tap, { pointerType: 'touch', clientX: 5, clientY: 5 }));
    document.dispatchEvent(fakeEvent('pointerup', tap, { pointerType: 'touch' }));
    assert.deepStrictEqual(seen.copied, [], 'a short release is not a long press');
    document.dispatchEvent(fakeEvent('click', tap));
    assert.strictEqual(seen.copied.length, 1, 'the tap copies via click');
    const scrolled = stampOf(H.cardFor(msg(29, PY_ISO)));
    document.dispatchEvent(fakeEvent('pointerdown', scrolled, { pointerType: 'touch', clientX: 5, clientY: 5 }));
    await hold();
    document.dispatchEvent(fakeEvent('pointermove', scrolled, { clientX: 5, clientY: 80 }));
    document.dispatchEvent(fakeEvent('pointerup', scrolled, { pointerType: 'touch' }));
    assert.strictEqual(seen.copied.length, 1, 'a hold that turned into a scroll copies nothing');
  });
  await check('Android contextmenu from touch arms the copy for the release; a mouse right-click keeps the browser menu', async () => {
    const seen = stubUi();
    const mouse = stampOf(H.cardFor(msg(22, PY_ISO)));
    document.dispatchEvent(fakeEvent('pointerdown', mouse, { pointerType: 'mouse' }));
    const right = fakeEvent('contextmenu', mouse);
    document.dispatchEvent(right);
    assert.strictEqual(right.prevented, false);
    const touch = stampOf(H.cardFor(msg(23, PY_ISO)));
    document.dispatchEvent(fakeEvent('pointerdown', touch, { pointerType: 'touch', clientX: 5, clientY: 5 }));
    const press = fakeEvent('contextmenu', touch);
    document.dispatchEvent(press);
    assert.ok(press.prevented, 'the native menu is suppressed');
    assert.deepStrictEqual(seen.copied, [], 'contextmenu has no user activation, so it does not copy');
    document.dispatchEvent(fakeEvent('pointerup', touch, { pointerType: 'touch' }));
    assert.deepStrictEqual(seen.copied, ['2026-10-08T14:25:12.262Z'], 'copied once, on the touch release only');
  });
  await check('a failed copy says so and still shows the instant to copy by hand', async () => {
    const seen = stubUi({ fail: true });
    const time = stampOf(H.cardFor(msg(24, PY_ISO)));
    document.dispatchEvent(fakeEvent('click', time));
    await tick();
    assert.ok(/Could not copy/.test(seen.toasts[0]) && seen.toasts[0].includes('2026-10-08T14:25:12.262Z'), seen.toasts[0]);
  });
  await check('a time without a parsable instant is inert', async () => {
    const seen = stubUi();
    const time = stampOf(H.cardFor(msg(25, 'garbage')));
    const ev = fakeEvent('click', time);
    document.dispatchEvent(ev);
    await tick();
    assert.deepStrictEqual(seen.copied, []);
    assert.strictEqual(ev.stopped, false);
  });

  // ── markup for innerHTML views (search, workspace cards, activity) ──────
  await check('html(): escaped, ISO datetime + title, optional day, not a tab stop inside a button', () => {
    setMode('local');
    const out = T.html(PY_ISO, { withDay: true, tabbable: false });
    assert.ok(out.startsWith('<time class="msg-time" datetime="2026-10-08T14:25:12.262Z" title="2026-10-08T14:25:12.262Z">'), out);
    assert.ok(out.endsWith(' 10:25:12</time>'), out);
    assert.ok(!out.includes('tabindex'));
    assert.strictEqual(T.html('<b>x</b>'), '<time class="msg-time">&lt;b&gt;x&lt;/b&gt;</time>');
    assert.strictEqual(T.html(''), '');
  });
  await check("withDay 'auto' shows the day only when it is not today", () => {
    const now = new Date('2026-10-08T16:00:00Z');
    assert.strictEqual(T.label(PY_ISO, { withDay: 'auto', now, mode: 'local' }), '10:25:12');
    assert.notStrictEqual(T.label('2026-10-06T14:25:12Z', { withDay: 'auto', now, mode: 'local' }), '10:25:12');
    const utc = T.label('2026-10-06T14:25:12Z', { withDay: 'auto', now, mode: 'utc' });
    assert.ok(utc.endsWith(' 14:25:12Z') && !utc.includes('UTC'), 'one zone marker: ' + utc);
  });
  await check('dateTime(): full date and time with a single zone marker', () => {
    const local = T.dateTime(PY_ISO, { mode: 'local' });
    const utc = T.dateTime(PY_ISO, { mode: 'utc' });
    assert.ok(local.endsWith(' 10:25:12') && local.length > ' 10:25:12'.length, local);
    assert.ok(utc.endsWith(' 14:25:12Z'), utc);
    assert.strictEqual((utc.match(/UTC|Z/g) || []).length, 1, 'exactly one zone marker: ' + utc);
    assert.strictEqual(T.dateTime('garbage'), 'garbage');
  });
  await check('day separators keep naming their zone in UTC mode', () => {
    assert.ok(T.day(PY_ISO, 'utc').endsWith(' UTC'));
    assert.ok(!T.day(PY_ISO, 'local').includes('UTC'));
  });

  // ── agent activity rows use the real Codex record shape ─────────────────
  // nth_codex_runtime._record_activity writes method / created_at /
  // turn_id / item_type / status / summary — and no ts, type or content.
  await check('activity rows read created_at, method/item_type and summary from Codex records', () => {
    setMode('local');
    const record = { method: 'item/completed', created_at: '2026-10-08T14:25:12+00:00', turn_id: 't1',
      item_type: 'commandExecution', status: 'completed', summary: 'npm test' };
    const html = Trio.agents.renderActivityEvent(record);
    assert.ok(html.includes('<time class="msg-time" datetime="2026-10-08T14:25:12.000Z"'), html);
    assert.ok(!html.includes('<time></time>'), 'the time is never empty for a timestamped record');
    assert.ok(html.includes('<b>Command · completed</b>'), html);
    assert.ok(html.includes('class="activity-event type-command"'), html);
    assert.ok(html.includes('>npm test</pre>'), html);
    const plan = Trio.agents.renderActivityEvent({ method: 'turn/plan/updated', created_at: '2026-10-08T14:25:12+00:00', item_type: '', status: '', summary: 'plan updated (3 steps)' });
    assert.ok(plan.includes('<b>Plan</b>') && plan.includes('plan updated (3 steps)'), plan);
    const other = Trio.agents.renderActivityEvent({ method: 'turn/started', created_at: '2026-10-08T14:25:12+00:00', item_type: '', status: 'inProgress' });
    assert.ok(other.includes('<b>turn/started · inProgress</b>') && !other.includes('<pre'), other);
  });

  Trio.preferences.reset();
  console.log(`\n${passed} passed, ${failures.length} failed`);
  if (failures.length) process.exit(1);
})();
