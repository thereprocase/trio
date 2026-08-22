// "Hide old threads" — the preference that moves quiet channels and DMs into a
// "show older" group in the sidebar.
//
// It began life as "Hide old messages", collapsing messages by age INSIDE a
// conversation, which was never the intent: the point was to stop long-dead
// channels and DMs sitting at full weight in the sidebar forever. That message
// collapse is gone (see test-unread.js) and the preference now classifies whole
// threads.
//
// The rules that matter are the ones about what must NEVER be hidden, because
// this is a view filter over things the operator may still need:
//   * unread is never stale — an old thread you have not read is the single
//     most likely thing in the list to actually want you
//   * the open thread is never stale — a row vanishing while you read it reads
//     as a bug, not as a preference
//   * a thread with no timestamp is never stale — absence of evidence is not
//     evidence of age, and a channel created seconds ago has none
//   * nothing is archived or deleted; the group is one click away
//
// Usage: node tests/test-stale-threads.js
'use strict';

const { load } = require('./dom-harness');

const failures = [];
let passed = 0;
function check(name, cond) {
  if (cond) { passed++; console.log('PASS: ' + name); }
  else { failures.push(name); console.log('FAIL: ' + name); }
}

const cx = load();
const Trio = cx.hooks.Trio;
const state = Trio.state;
const DAY = 86400000;
const ago = d => new Date(Date.now() - d * DAY).toISOString();

function setup({ days = 14, channels = [], dms = [], openChannel = '', openDm = '' } = {}) {
  state.preferences = Object.assign({}, state.preferences, { staleThreadDays: days });
  state.channels = channels;
  state.dms = { your_dms: dms, agent_dms: [] };
  state.channel = openChannel;
  state.dmKey = openDm;
  state.staleOpen = {};
}
const nav = () => Trio.workspace.groupNavigation(state.channels, state.dms);
const ids = list => list.map(x => x.code || x.key).sort();

// ── the core behaviour ──────────────────────────────────────────────────────
setup({ days: 14, channels: [
  { code: 'busy', last_at: ago(1), unread: 0 },
  { code: 'dead', last_at: ago(90), unread: 0 },
]});
check('a channel active within the window stays', ids(nav().active).includes('busy'));
check('a long-quiet channel becomes stale', ids(nav().staleChannels).includes('dead'));
check('stale channels are removed from the active list',
      !ids(nav().active).includes('dead'));

// Nothing is destroyed — every channel is still accounted for somewhere.
check('every channel is still present across the two groups',
      ids([...nav().active, ...nav().staleChannels]).join() === 'busy,dead');

// ── what must never be hidden ───────────────────────────────────────────────
setup({ days: 14, channels: [{ code: 'old-unread', last_at: ago(90), unread: 3 }] });
check('an unread channel is never stale, however old', ids(nav().active).includes('old-unread'));

setup({ days: 14, channels: [{ code: 'reading', last_at: ago(90), unread: 0 }],
        openChannel: 'reading' });
check('the channel you are reading is never stale', ids(nav().active).includes('reading'));

setup({ days: 14, dms: [{ key: 'k1', last_at: ago(90), unread: 0 }], openDm: 'k1' });
check('the DM you are reading is never stale', ids(nav().yours).includes('k1'));

setup({ days: 14, channels: [{ code: 'brand-new', unread: 0 }] });
check('a channel with no timestamp is never stale',
      ids(nav().active).includes('brand-new'));
setup({ days: 14, channels: [{ code: 'bad-date', last_at: 'not-a-date', unread: 0 }] });
check('an unparseable timestamp is never stale',
      ids(nav().active).includes('bad-date'));

// ── DMs follow the same rule ────────────────────────────────────────────────
setup({ days: 14, dms: [
  { key: 'recent', last_at: ago(2), unread: 0 },
  { key: 'ancient', last_at: ago(60), unread: 0 },
  { key: 'ancient-unread', last_at: ago(60), unread: 1 },
]});
check('a quiet DM becomes stale', ids(nav().staleDms).includes('ancient'));
check('a recent DM stays', ids(nav().yours).includes('recent'));
check('an unread DM stays however old', ids(nav().yours).includes('ancient-unread'));

// ── the boundary ────────────────────────────────────────────────────────────
setup({ days: 14, channels: [
  { code: 'just-inside', last_at: ago(13), unread: 0 },
  { code: 'just-outside', last_at: ago(15), unread: 0 },
]});
check('a thread just inside the window is kept', ids(nav().active).includes('just-inside'));
check('a thread just outside the window is stale', ids(nav().staleChannels).includes('just-outside'));

// ── "Never" turns the whole thing off ───────────────────────────────────────
setup({ days: 0, channels: [{ code: 'ancient', last_at: ago(999), unread: 0 }],
        dms: [{ key: 'ancient-dm', last_at: ago(999), unread: 0 }] });
check('days = 0 (Never) keeps every channel', ids(nav().active).includes('ancient'));
check('days = 0 (Never) keeps every DM', ids(nav().yours).includes('ancient-dm'));
check('days = 0 (Never) produces no stale group',
      !nav().staleChannels.length && !nav().staleDms.length);

// A missing/garbage preference must also mean "off" rather than hiding
// everything with a date — the failure mode has to be inert.
state.preferences = Object.assign({}, state.preferences, { staleThreadDays: 'banana' });
check('a non-numeric preference disables the filter', Trio.workspace.staleThreadDays() === 0);
delete state.preferences.staleThreadDays;
check('a missing preference disables the filter rather than hiding everything',
      Trio.workspace.staleThreadDays() === 0
      || !Trio.workspace.isStaleThread({ last_at: ago(999) }, ''));

// ── the offered choices ─────────────────────────────────────────────────────
// Pinned because the spread is a judgement about how this deployment is used,
// not an implementation detail: a channel here is usually one task-scoped
// discussion with one agent, and those go quiet in DAYS. An option list that
// starts at a week cannot express that, so 3 has to stay on it.
const prefsSrc = require('fs').readFileSync(
  require('path').join(__dirname, '..', 'server', 'web', 'js', '40-preferences.js'), 'utf8');
const optionLine = (prefsSrc.match(/const historyOptions = \[.*\];/) || [''])[0];
const offered = [...optionLine.matchAll(/\[(\d+),/g)].map(m => Number(m[1]));
check('3 days is offered — task-scoped channels go quiet in days',
      offered.includes(3));
check('the spread is 3/7/14/30 plus Never',
      offered.join(',') === '3,7,14,30,0');
check('the default is one of the offered values',
      offered.includes(Number(Trio.preferences.read().staleThreadDays ?? 7)));

// ── archived is a separate axis ─────────────────────────────────────────────
setup({ days: 14, channels: [
  { code: 'arch', last_at: ago(90), archived: true },
  { code: 'live-old', last_at: ago(90), unread: 0 },
]});
check('archived channels stay in the archived group, not the stale one',
      ids(nav().archived).includes('arch') && !ids(nav().staleChannels).includes('arch'));
check('a stale channel is NOT archived — the filter changes no state',
      ids(nav().staleChannels).includes('live-old') && !ids(nav().archived).includes('live-old'));

console.log();
if (failures.length) {
  console.log(`FAILED — ${failures.length} of ${failures.length + passed}`);
  failures.forEach(f => console.log('  - ' + f));
  process.exit(1);
}
console.log(`OK — ${passed} passed`);
