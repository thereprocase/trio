const fs = require('fs');
const vm = require('vm');
const path = require('path');
// Resolved against THIS FILE, not the working directory: tests/run-all.sh
// runs from tests/, and a cwd-relative path made these pass standalone
// from the repo root while failing under the runner.
const WEB_JS = n => path.resolve(__dirname, '..', 'server', 'web', 'js', n);

function baseContext(search = '', pathname = '/') {
  const context = {
    window: {},
    location: { search, pathname, href: 'http://localhost' + pathname + search },
    URLSearchParams,
    document: {
      getElementById: () => null,
      querySelectorAll: () => [],
      body: { classList: { toggle() {} } },
      documentElement: { dataset: {} },
    },
    localStorage: { getItem: () => null, setItem() {} },
    fetch: () => Promise.resolve({ ok: false, status: 404 }),
    EventTarget: class { addEventListener() {} dispatchEvent() {} },
    CustomEvent: class {},
    console,
    setInterval() {},
  };
  context.window = context;
  context.globalThis = context;
  vm.createContext(context);
  return context;
}

function loadModule(name, context) {
  vm.runInContext(fs.readFileSync(WEB_JS(name), 'utf8'), context);
}

// Base workspace tests run with the default empty search.
const base = baseContext();
loadModule('01-store.js', base);
loadModule('02-api.js', base);
loadModule('05-loader.js', base);
loadModule('04-events.js', base);
loadModule('00-core.js', base);
loadModule('06-ui.js', base);
loadModule('20-workspace.js', base);
loadModule('40-preferences.js', base);

let failures = 0;
function check(name, cond) {
  if (cond) { console.log('PASS: ' + name); }
  else { console.log('FAIL: ' + name); failures++; }
}

const nav = base.Trio.workspace.groupNavigation([{ code: 'a' }, { code: 'b', archived: true }], { your_dms: [{ key: 'x' }] });
check('navigation grouping', nav.active.length === 1 && nav.archived.length === 1 && nav.yours.length === 1);
check('attention count', base.Trio.workspace.attentionCount({ approvals: [{}, {}] }) === 2);

const s = base.Trio.workspace.selectors;
check('selector attention', s.attention({ approvals: [{}, {}], questions: [{}, {}] }) === 4);
check('selector pending questions', s.pendingQuestions({ questions: [{}, {}] }) === 2);
check('selector unread mentions', s.unreadMentions({ mentions: [{}, {}, {}] }) === 3);
check('selector blocked tasks', s.blockedTasks({ tasks: [{ status: 'open' }, { status: 'blocked' }, { status: 'done' }] }) === 1);
check('selector pending approvals', s.pendingApprovals({ approvals: [{ status: 'open' }, { status: 'resolved' }] }) === 1);
check('selector open tasks', s.openTasks({ tasks: [{ status: 'open' }, { status: 'blocked' }, { status: 'done' }] }) === 2);
check('selector blocked agents', s.blockedAgents({ agents: [{ status: 'error' }, { status: 'working' }] }) === 1);
check('selector unread dms', s.unreadDms({ dms: { your_dms: [{ unread: 3 }, { unread: 0 }, {} ] } }) === 3);
check('selector recent channels', s.recentChannels({ channels: [{ code: 'x' }, { code: 'y', archived: true }, { code: 'z' }] }).length === 2);

// Live-activity indicators (face-pile + channel-drawer): channelStatus must
// prefer the agent-roster {live, busy, state} shape when present so those two
// views agree with the Agent roster page, and toolSuffix must only surface
// the last-tool hint while genuinely mid-turn (not on a stale finished turn).
const cs = base.Trio.workspace.channelStatus;
check('channelStatus: roster-shaped busy agent is working', cs({ live: true, busy: true, state: 'active' }) === 'working');
check('channelStatus: roster-shaped idle agent is idle', cs({ live: true, busy: false, state: 'active' }) === 'idle');
check('channelStatus: roster-shaped dead agent falls back to offline', cs({ live: false, state: 'stopped' }) === 'offline');
check('channelStatus: heartbeat-shaped member reports its own status', cs({ status: 'working' }) === 'working');
check('channelStatus: unrecognized status falls back to offline', cs({ status: 'bogus' }) === 'offline');
// LOTC/Sauron: a bare identity object (the operator's {id,name,source} shape
// carries no status/live/state at all) must not silently read as offline —
// renderFacePile special-cases the operator by id rather than relying on
// channelStatus here, but channelStatus's own default for a field-less
// object is still worth locking in explicitly.
check('channelStatus: field-less identity object defaults to offline', cs({ id: 'op', name: 'You' }) === 'offline');
// A DM-merged object can carry both shapes at once ({...rosterMember, ...agent}
// in the channel-drawer) — the roster shape (live/state) must win over a
// possibly-stale heartbeat `status` field, since it's the fresher source.
check('channelStatus: mixed shapes prefer the live roster fields over a stale heartbeat status', cs({ status: 'idle', live: true, busy: true, state: 'active' }) === 'working');

const ts = base.Trio.workspace.toolSuffix;
check('toolSuffix: shows the live tool while working', ts({ last_tool_name: 'Bash', last_tool_target: 'npm test' }, 'working') === ' — using Bash: npm test');
check('toolSuffix: omits target when absent', ts({ last_tool_name: 'Read' }, 'working') === ' — using Read');
check('toolSuffix: hidden once the turn has ended (stale trivia)', ts({ last_tool_name: 'Bash' }, 'idle') === '');
check('toolSuffix: hidden when no tool activity recorded', ts({}, 'working') === '');

// Context-fullness + usage-quota indicators (nth_supervisor persists
// context_pct from the last `assistant` event's own token usage — NOT the
// turn-level `result` event, whose usage is accumulated across every
// internal API call the turn made and would overcount; /api/usage feeds
// the home screen from Claude Code's own statusline-state.json).
const usageTone = base.Trio.workspace.usageTone;
check('usageTone: under 70% is ok', usageTone(50) === 'ok');
check('usageTone: 70-89% is warn', usageTone(75) === 'warn');
check('usageTone: 90%+ is danger', usageTone(95) === 'danger');
check('usageTone: boundary at exactly 70 is warn', usageTone(70) === 'warn');
check('usageTone: boundary at exactly 90 is danger', usageTone(90) === 'danger');

// LOTC/Frodo: "% ctx" alone was directionally ambiguous (used vs.
// remaining) — "% full" states the direction in the badge face itself.
const contextBadge = base.Trio.workspace.contextBadge;
check('contextBadge: renders the rounded percentage', contextBadge({ context_pct: 42.6 }).includes('43% full'));
check('contextBadge: colors by usageTone', contextBadge({ context_pct: 95 }).includes('danger'));
check('contextBadge: empty when context_pct is unknown (human, unspawned agent)', contextBadge({}) === '');
check('contextBadge: renders "0% full" (not treated as missing) when context_pct is exactly 0', contextBadge({ context_pct: 0 }).includes('0% full'));

// Floors (never rounds up) so the label is always a safe lower bound —
// +5h+30s still reads "5h" rather than drifting to "4h" or "6h" depending
// on exactly when the check runs.
const resetLabel = base.Trio.workspace.resetLabel;
check('resetLabel: empty for a falsy timestamp', resetLabel(0) === '' && resetLabel(null) === '');
check('resetLabel: under an hour shows minute precision', resetLabel(Math.floor(Date.now() / 1000) + 1830) === 'resets in 30m');
check('resetLabel: hours-away format', resetLabel(Math.floor(Date.now() / 1000) + 3600 * 5 + 30).startsWith('resets in 5h'));
check('resetLabel: days-away format', resetLabel(Math.floor(Date.now() / 1000) + 3600 * 48 + 30).includes('resets in 2d'));
check('resetLabel: already passed reads as "resets now", distinct from "within the hour" (clock skew safety)',
      resetLabel(Math.floor(Date.now() / 1000) - 100) === 'resets now');

// Channel-size indicator: 2-significant-figure K/M formatting per the
// product spec (1.2K, 12K, 120K, 1.2M, 12M). Number.toPrecision(2) alone
// would render 3-digit values in exponential notation (e.g. "1.2e+2"
// instead of "120") — formatTokenEstimate must avoid that.
const formatTokenEstimate = base.Trio.workspace.formatTokenEstimate;
check('formatTokenEstimate: sub-thousand values render as a plain integer', formatTokenEstimate(500) === '500');
check('formatTokenEstimate: low thousands round to 2 sig figs with a K suffix', formatTokenEstimate(1234) === '1.2K');
check('formatTokenEstimate: mid thousands round to 2 sig figs with a K suffix', formatTokenEstimate(12345) === '12K');
check('formatTokenEstimate: high thousands round to 2 sig figs without exponential notation', formatTokenEstimate(123456) === '120K');
check('formatTokenEstimate: low millions round to 2 sig figs with an M suffix', formatTokenEstimate(1234567) === '1.2M');
check('formatTokenEstimate: mid millions round to 2 sig figs with an M suffix', formatTokenEstimate(12345678) === '12M');
check('formatTokenEstimate: zero renders as "0"', formatTokenEstimate(0) === '0');

// LOTC/Frodo: "Messages loaded" (formerly mislabeled "Messages today") is
// state.messages.size, which 11-conversation.js's pruneMessages(500) caps —
// a busy channel pins at 500 and stops moving even as more arrive. "500+"
// makes that cap visible instead of the counter looking frozen/broken.
const messageCountLabel = base.Trio.workspace.messageCountLabel;
base.Trio.state.messages = new Map([[1, {}], [2, {}], [3, {}]]);
check('messageCountLabel: shows the exact count under the cap', messageCountLabel() === '3');
base.Trio.state.messages = new Map(Array.from({ length: 500 }, (_, i) => [i, {}]));
check('messageCountLabel: shows "500+" at the pruneMessages cap, not a frozen "500"', messageCountLabel() === '500+');
base.Trio.state.messages = new Map();
check('messageCountLabel: empty state reads "0"', messageCountLabel() === '0');

// Theme presets. These assertions used to hardcode a count (five each) and two
// preset ids that no longer exist, so they went red the day the palette was
// renamed and said nothing about whether theme selection still WORKED. The
// contract worth pinning is the partition and the round trip, neither of which
// cares how many presets there are or what they are called.
const lights = base.Trio.preferences.lightThemes;
const darks = base.Trio.preferences.darkThemes;
check('every preset is either light or dark, and both modes are offered',
  lights.length > 0 && darks.length > 0
  && lights.every(t => t.mode === 'light') && darks.every(t => t.mode === 'dark'));
check('no preset id appears in both modes',
  lights.every(l => !darks.some(d => d.id === l.id)));

const aLight = lights[lights.length - 1].id;   // not the default, so a no-op shows
const aDark = darks[darks.length - 1].id;
base.Trio.preferences.selectTheme(aLight);
check(`selecting a light preset (${aLight}) saves it as the light default`,
  base.Trio.preferences.read().lightTheme === aLight && base.Trio.preferences.read().theme === aLight);
base.Trio.preferences.selectTheme(aDark);
check(`selecting a dark preset (${aDark}) saves it as the dark default`,
  base.Trio.preferences.read().darkTheme === aDark && base.Trio.preferences.read().theme === aDark);
// The toggle must return to the light preset the user CHOSE, not to the
// shipped default — the whole point of storing lightTheme separately.
base.Trio.preferences.toggle();
check('theme toggle returns to the saved light preset, not the built-in default',
  base.Trio.preferences.read().theme === aLight);

base.Trio.state.dms = { targets: [{ id: 'z', name: 'Zed' }, { id: 'a', name: 'Ada' }] };
check('direct-message targets are sorted by display name', base.Trio.workspace.dmTargets().map(target => target.id).join(',') === 'a,z');

const routerContext = baseContext('', '/tasks');
loadModule('01-store.js', routerContext);
loadModule('03-router.js', routerContext);
check('page path parses as tasks route', routerContext.Trio.router.parse('', '/tasks').name === 'tasks');
check('page route serializes to tasks path', routerContext.Trio.router.serialize({ name: 'tasks', params: {} }) === '/tasks');
check('inbox path maps to attention route', routerContext.Trio.router.parse('', '/inbox').name === 'attention');
check('agent path maps to roster route', routerContext.Trio.router.parse('', '/agents').name === 'roster');
check('settings path maps to preferences route', routerContext.Trio.router.parse('', '/settings').name === 'prefs');
check('conversation query takes precedence over page path', routerContext.Trio.router.parse('?channel=general', '/tasks').name === 'channel');

// Agent-audit clicks are read-only, but they are not archived threads. The
// client must preserve that distinction in the history request.
base.document.querySelector = () => null;
const auditElements = new Map(['h-channel', 'h-meta', 'private-banner'].map(id => [id, {
  classList: { toggle() {} },
  set textContent(value) { this._textContent = value; },
  get textContent() { return this._textContent || ''; },
} ]));
base.document.getElementById = id => auditElements.get(id) || null;
base.Trio.startEvents = () => {};
base.Trio.conversation = { render() {}, upsert() {} };
base.Trio.loader = { cancel() {}, load(_name, fn) { return fn({}); } };
let auditRoute = null;
const auditRequests = [];
base.Trio.router = { navigate(name, params) { auditRoute = { name, params }; } };
base.Trio.api.get = path => { auditRequests.push(path); return Promise.resolve({ messages: [] }); };
base.Trio.workspace.openDm({ key: 'ag_a,ag_b', member_ids: ['ag_a', 'ag_b'], name: 'A ↔ B' }, false, true);
check('agent audit requests active history', auditRequests[0] === '/api/dms?with=ag_a%2Cag_b');
check('agent audit uses audit route', auditRoute?.name === 'audit' && !auditRoute.params.archived);

// URL routing contract tests.
function routeFor(search) {
  const cx = baseContext(search);
  loadModule('01-store.js', cx);
  loadModule('02-api.js', cx);
  loadModule('04-events.js', cx);
  loadModule('00-core.js', cx);
  return cx.Trio.state.conversation;
}

// --- createChannel: failure must not vanish silently -------------------
// The create modal is a method="dialog" form that closes the instant Save
// is clicked, before the async POST resolves. A failed create used to
// disappear ("modal closed, no channel"). createChannel now surfaces the
// error and reopens the form pre-filled so the operator can retry.
(function testCreateChannel() {
  // FormData polyfill: the vm context has none. Backed by a fake form whose
  // fields the test sets to mimic operator input (incl. stray whitespace/case).
  base.FormData = class {
    constructor(form) { this._f = form._fields || {}; }
    get(name) { return name in this._f ? this._f[name] : null; }
  };
  const fakeNode = fields => ({ querySelector: () => ({ _fields: fields }) });

  // Capture every modal() call: (title, body, submitCallback).
  const opened = [];
  base.Trio.ui.modal = (title, body, submit) => { opened.push({ title, body, submit }); };
  const toasts = [];
  base.Trio.ui.toast = m => toasts.push(m);

  const createChannel = base.Trio.workspace.createChannel;

  // Case 1: server rejects -> toast the real error AND reopen pre-filled.
  let postArgs = null;
  base.Trio.api.post = (path, body) => { postArgs = { path, body }; return Promise.reject(new Error('503 /api/channels: busy')); };
  opened.length = 0; toasts.length = 0;
  createChannel();
  check('createChannel: opens the create modal', opened.length === 1);
  // Operator typed a code with stray case/space; submit it.
  return opened[0].submit(fakeNode({ code: '  Global-Logout ', topic: ' hi ' })).then(() => {
    check('createChannel: code is trimmed + lowercased before POST',
      postArgs && postArgs.body.code === 'global-logout' && postArgs.body.topic === 'hi');
    check('createChannel: failure surfaces the server error as a toast',
      toasts.length === 1 && /503/.test(toasts[0]));
    check('createChannel: failure reopens the modal (does not vanish)',
      opened.length === 2);
    check('createChannel: reopened modal is pre-filled with the entered values',
      /value="global-logout"/.test(opened[1].body) && /value="hi"/.test(opened[1].body));

    // Case 2: success path posts once and does NOT reopen.
    let posts = 0;
    base.Trio.api.post = (path, body) => { posts++; postArgs = { path, body }; return Promise.resolve({ ok: true }); };
    opened.length = 0; toasts.length = 0;
    createChannel();
    return opened[0].submit(fakeNode({ code: 'units-pref-subpage', topic: '' })).then(() => {
      check('createChannel: success posts exactly once', posts === 1);
      check('createChannel: success does not reopen the modal or toast an error',
        opened.length === 1 && toasts.length === 0);
    });
  });
})().then(() => {

const cases = [
  ['empty query', '', { kind: 'unknown', key: '' }],
  ['channel only', '?channel=general', { kind: 'channel', key: 'general' }],
  ['dm only', '?dm=ag_123', { kind: 'dm', key: 'ag_123' }],
  ['dm with comma-joined keys', '?dm=ag_123,ag_456', { kind: 'dm', key: 'ag_123,ag_456' }],
  ['dm with encoded comma', '?dm=ag_123%2Cag_456', { kind: 'dm', key: 'ag_123,ag_456' }],
  ['dm and channel', '?channel=general&dm=ag_123', { kind: 'dm', key: 'ag_123' }],
  ['empty dm value', '?dm=', { kind: 'unknown', key: '' }],
  ['dm with forbidden characters', '?dm=ag_123!bad', { kind: 'unknown', key: '' }],
  ['dm with slash', '?dm=ag/123', { kind: 'unknown', key: '' }],
];
for (const [name, search, expected] of cases) {
  const actual = routeFor(search);
  check(name, actual.kind === expected.kind && actual.key === expected.key);
}

console.log((failures ? 'FAILED' : 'OK') + ' — ' + failures + ' failure(s)');
process.exit(failures ? 1 : 0);

}).catch(err => { console.log('FAIL: createChannel test threw — ' + err.stack); process.exit(1); });
