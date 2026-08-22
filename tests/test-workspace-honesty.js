// The workspace must not tell the operator that things are fine when they are not.
//
// Three separate defects with one shape: the UI reports success or health it
// has not established. None of them throws, none logs, and each looks correct
// in isolation — which is why they are pinned here rather than left to review.
//
//   1. Home's error branch was gated on `!dataReady('channels')`, but refresh()
//      marks EVERY slice loaded once it settles (deliberately — otherwise a
//      partial failure leaves sections spinning). So the branch was
//      unreachable, and with every request failing Home rendered a greeting,
//      three zeros and a green health row.
//   2. The Runtime health chips were the literal string 'ok' in all three
//      cases, never computed. The row could not express bad health at all.
//   3. archiveCurrent built its prompt as "Archive …?" before computing the
//      direction, so RESTORING an archived conversation asked the operator to
//      confirm archiving it.
//
// Plus the confirmation buttons: every destructive dialog in the app was
// accepted by a button labelled "Save".
//
// Usage: node tests/test-workspace-honesty.js
'use strict';

const { load } = require('./dom-harness');

const failures = [];
let passed = 0;
function check(name, cond, detail = '') {
  if (cond) { passed++; console.log('PASS: ' + name); }
  else { failures.push(name); console.log('FAIL: ' + name + (detail ? ' — ' + detail : '')); }
}

const cx = load();
const Trio = cx.hooks.Trio;
const document = cx.document;
if (cx.bootError) console.log('(note) boot ran with: ' + cx.bootError.message);

const state = Trio.state;

(async () => {

// showView() is the real entry point; it renders Home into #trio-home-view.
// Driving it (rather than calling renderHome directly) means the test exercises
// the same path the app does — including showView's own state resets.
function renderHome() {
  Trio.workspace.showView('home');
  return document.getElementById('trio-home-view');
}

// ── 1. A totally failed refresh must SAY SO ─────────────────────────────────
// Reproduces the real post-refresh state: every slice marked loaded (because
// refresh() does that unconditionally) AND an error recorded.
state.workspaceError = 'Workspace refresh failed';
state.loaded = { channels: true, dms: true, meta: true, tasks: true,
                 approvals: true, questions: true, mentions: true,
                 usage: true, agents: true };
state.channels = [];
state.dms = { your_dms: [], agent_dms: [] };
state.tasks = []; state.approvals = []; state.questions = []; state.mentions = [];
state.usage = {}; state.agents = [];

let home = renderHome();
let text = home.textContent || '';
check('a refresh where everything failed shows the error, not a healthy Home',
      text.includes('Workspace refresh failed'), JSON.stringify(text.slice(0, 120)));
check('…and offers a Retry',
      [...home.querySelectorAll('button')].some(b => /retry/i.test(b.textContent)));
check('…and does NOT still greet the operator as if all were well',
      !/Good (morning|afternoon|evening)/.test(text));

// The inverse: a failure that still has data to show must NOT blank the page.
state.channels = [{ code: 'deploy', topic: 'Shipping', members: [], unread: 0 }];
home = renderHome();
text = home.textContent || '';
check('a PARTIAL failure still renders the workspace it does have',
      text.includes('deploy') && !text.startsWith('Workspace refresh failed'));

// ── 2. Runtime health must be derived ───────────────────────────────────────
state.workspaceError = '';
state.sliceErrors = {};
// showView() calls stopEvents(), which then honestly reports the stream as
// offline — so the Hub chip is correctly warn in a bare harness. To assert the
// all-healthy case the fixture has to keep the connection up across the render,
// which means neutralising that one call. (The fact that this is necessary is
// itself the point: the chip now follows the real connection state.)
const realStop = Trio.stopEvents;
Trio.stopEvents = () => {};
Trio.store.set('connection', { text: 'live', failed: false });
home = renderHome();
// Two-step rather than a descendant selector: the DOM harness's
// querySelector is deliberately minimal and does not implement them.
const okDots = [...home.querySelectorAll('.hchip')]
  .map(c => c.querySelector('.d')?.className || '');
check(`healthy state shows healthy chips (${okDots.join(', ')})`,
      okDots.length >= 3 && okDots.every(c => /\bok\b/.test(c)));

Trio.stopEvents = realStop;

// Now make the slices behind those chips fail.
state.sliceErrors = { agents: new Error('409'), channels: new Error('500') };
home = renderHome();
const chips = [...home.querySelectorAll('.hchip')];
// Asserted PER CHIP, not "some chip is warn" — with `some`, hardcoding any one
// chip back to green still passed because a neighbour was warn. Each chip is a
// separate claim about a separate subsystem and has to be checked as one.
const byName = {};
for (const c of chips) {
  const label = (c.textContent || '').split('\u00b7')[0].trim();
  byName[label] = { tone: c.querySelector('.d')?.className || '',
                    text: c.textContent || '' };
}
check(`the row names the subsystems it claims to cover `
      + `(${Object.keys(byName).join(', ')})`,
      ['Hub', 'Agents', 'Database'].every(k => k in byName));
for (const [label, slice] of [['Agents', 'agents'], ['Database', 'channels']]) {
  const chip = byName[label] || {};
  check(`the ${label} chip goes non-ok when its own slice (${slice}) failed `
        + `(tone="${chip.tone}") — a chip that is always green argues the `
        + 'operator out of investigating',
        /\bwarn\b/.test(chip.tone || ''));
  check(`…and the ${label} chip says so in words (${(chip.text || '').trim()})`,
        /unavailable|unreachable/.test(chip.text || ''));
}

// ── 2b. …and the failures must be ATTRIBUTED to the right slice ────────────
// The health chips read state.sliceErrors, which refresh() fills by zipping a
// name list against Promise.allSettled results. That correspondence is
// positional and silent: if the list drifts out of step with the request array,
// every chip still renders, just blaming the wrong subsystem. Setting
// sliceErrors by hand (as above) cannot catch that, so drive the real refresh.
{
  const realGet = Trio.api.get;
  const realAgents = Trio.agents && Trio.agents.refresh;
  // Fail exactly one endpoint. If the zip is correct, exactly that slice is
  // marked — if it is off by one, a different slice is.
  Trio.api.get = (path, ...rest) => path.startsWith('/api/tasks')
    ? Promise.reject(new Error('boom'))
    : Promise.resolve({ ok: true, channels: [], tasks: [], mentions: [],
                        approvals: [], questions: [] });
  if (Trio.agents) Trio.agents.refresh = () => Promise.resolve();
  state.workspaceLoading = false;
  await Trio.workspace.refresh();
  const marked = Object.keys(state.sliceErrors || {});
  check(`a failing endpoint is attributed to its OWN slice (marked: `
        + `${marked.join(', ') || 'none'})`,
        marked.length === 1 && marked[0] === 'tasks');

  // A second slice at a different position. One probe cannot detect a
  // reordering of the name list that happens to leave that one probe's index
  // alone — swapping the first two entries passed the check above untouched.
  for (const [endpoint, expected] of [['/api/channels', 'channels'],
                                      ['/api/dms', 'dms'],
                                      ['/api/usage', 'usage']]) {
    Trio.api.get = (path) => path.startsWith(endpoint)
      ? Promise.reject(new Error('boom'))
      : Promise.resolve({ ok: true, channels: [], tasks: [], mentions: [],
                          approvals: [], questions: [] });
    state.workspaceLoading = false;
    await Trio.workspace.refresh();
    const got = Object.keys(state.sliceErrors || {});
    check(`failing ${endpoint} marks "${expected}" and nothing else `
          + `(marked: ${got.join(', ') || 'none'})`,
          got.length === 1 && got[0] === expected);
  }
  Trio.api.get = realGet;
  if (Trio.agents) Trio.agents.refresh = realAgents;
}

// ── 3. The archive prompt must match the direction ──────────────────────────
const prompts = [];
const realConfirm = Trio.ui.confirmAction;
Trio.ui.confirmAction = (message, ...rest) => { prompts.push(message); };
try {
  state.channel = 'deploy'; state.dmKey = '';
  state.readOnly = false;
  Trio.workspace.archiveCurrent();
  check(`archiving a live channel asks to Archive (${prompts[0]})`,
        /^Archive /.test(prompts[0] || ''));

  state.readOnly = true;          // i.e. the conversation is already archived
  Trio.workspace.archiveCurrent();
  check(`RESTORING an archived channel asks to Restore, not Archive `
        + `(${prompts[1]})`,
        /^Restore /.test(prompts[1] || ''));
} finally {
  Trio.ui.confirmAction = realConfirm;
}

// ── 4. A destructive button must name its action ────────────────────────────
function buttonLabelFor(message, options) {
  Trio.ui.confirmAction(message, () => {}, options);
  const dialog = document.getElementById('trio-control-modal');
  // The accept button is the only .primary in the dialog; an attribute
  // selector would need harness support the stub does not have.
  const submit = dialog.querySelector('.primary');
  return submit ? submit.textContent : '(none)';
}
const del = buttonLabelFor('Permanently delete the deploy channel?',
                           { submitLabel: 'Delete', danger: true });
check(`an explicit submitLabel is used (${del})`, del === 'Delete');
const inferred = buttonLabelFor('Archive this channel?');
check(`without one, the button follows the prompt's verb rather than "Save" `
      + `(${inferred})`,
      inferred === 'Archive');

// ── 5. Error text must read as a sentence, not a stack trace ───────────────
{
  const cases = [
    [403, 'not a trusted operator', /guest|trusted/i, 'a 403 explains the trust tier'],
    [409, 'managed agents are disabled on this server', /managed agents/i,
     "a server's own sentence is preferred over a generic one"],
    [413, '', /too large/i, 'a 413 with no detail still says what went wrong'],
    [404, '/api/whatever', /no longer here/i,
     'a bare path is rejected as machine text, not shown to a person'],
    [500, '500 /api/x', /error/i, 'a status echo is rejected as machine text'],
  ];
  for (const [status, detail, want, label] of cases) {
    const msg = Trio.api.humanize(status, detail);
    check(`${label} (${status} -> "${msg}")`, want.test(msg));
    check(`  …and it does not leak the endpoint path (${status})`,
          !/\/api\//.test(msg));
  }
}

// …and the request layer must actually USE it. Calling humanize() directly
// proves the mapping; it does not prove request() calls it, and reverting the
// call site alone survived a version of this test that stopped there.
{
  const realFetch = globalThis.fetch;
  globalThis.fetch = () => Promise.resolve({
    ok: false, status: 403,
    text: async () => JSON.stringify({ error: 'not a trusted operator' }),
  });
  cx.window.fetch = globalThis.fetch;
  try {
    let thrown = null;
    try { await Trio.api.get('/api/send'); } catch (e) { thrown = e; }
    check(`a real failed request throws a human message (${thrown && thrown.message})`,
          thrown && !/^\d{3} \/api\//.test(thrown.message));
    check('…and keeps the machine detail on the error for the console',
          thrown && thrown.status === 403 && /trusted/.test(thrown.detail || ''));
  } finally {
    globalThis.fetch = realFetch;
    cx.window.fetch = realFetch;
  }
}

// ── 6. Search must not claim an empty workspace it never searched ──────────
// Asserted on state.searchNotice rather than on the rendered list: dom-harness
// documents that a DOCUMENT-level querySelector always returns null, so
// reading the results panel here would test the harness, not the client.
// renderSearchResults branches on exactly this value, one line above the
// "No results." branch it has to pre-empt.
{
  const realFetch = globalThis.fetch;
  let fetched = 0;
  globalThis.fetch = () => { fetched++; return Promise.reject(new Error('boom')); };
  cx.window.fetch = globalThis.fetch;
  try {
    Trio.workspace.search();                 // opens the dialog

    // A one-character query is REJECTED BY THE SERVER (400, min 2 chars). It
    // used to render as "No results." — a claim about the operator's own
    // workspace that the search never actually made.
    await Trio.workspace.doSearch('a');
    check(`a 1-character query says to keep typing (${state.searchNotice})`,
          /at least/i.test(state.searchNotice || ''));
    check('…and does not spend a request finding that out', fetched === 0);

    // A search that FAILS is also not an empty workspace.
    await Trio.workspace.doSearch('deploy');
    check(`a failed search says it is unavailable rather than reporting zero `
          + `hits (${state.searchNotice})`,
          /unavailable/i.test(state.searchNotice || ''));
    check('…and it did reach the network for that one', fetched === 1);

    // A successful search clears the notice, or the panel would keep showing
    // the last failure over real results.
    globalThis.fetch = () => Promise.resolve({
      ok: true, json: async () => ({ results: [] }) });
    cx.window.fetch = globalThis.fetch;
    await Trio.workspace.doSearch('deploy');
    check(`a successful search clears the notice so real results can show `
          + `(${JSON.stringify(state.searchNotice)})`,
          !state.searchNotice);
  } finally {
    globalThis.fetch = realFetch;
    cx.window.fetch = realFetch;
  }
}

// ── 7. "No agents match" vs "this server has no agents" ────────────────────
// A hub without the agent supervisor answers 409 to every agent call. The
// roster swallowed that and printed "No agents match — try another filter",
// which sends the operator round the filters and the search box; the only way
// to learn the truth was to click "New agent" and read the toast.
{
  const realGet = Trio.api.get;
  const panel = document.createElement('div');
  try {
    const err409 = Object.assign(new Error('That is not enabled on this server.'),
                                 { status: 409 });
    Trio.api.get = () => Promise.reject(err409);
    await Trio.agents.refresh();
    Trio.agents.renderPage(panel);
    let text = panel.textContent || '';
    check(`a 409 roster says managed agents are off (${text.slice(0, 60).trim()})`,
          /managed agents are off/i.test(text));
    check('…and does NOT blame the operator\'s filter',
          !/No agents match/i.test(text));

    // A different failure is neither "off" nor "no match".
    Trio.api.get = () => Promise.reject(Object.assign(new Error('The server hit an error handling that.'), { status: 500 }));
    await Trio.agents.refresh();
    Trio.agents.renderPage(panel);
    text = panel.textContent || '';
    check(`a non-409 failure reports a load error (${text.slice(0, 60).trim()})`,
          /could not load/i.test(text) && !/No agents match/i.test(text));

    // Recovery must clear it, or the page keeps lying after the hub comes back.
    Trio.api.get = () => Promise.resolve({ ok: true, agents: [] });
    await Trio.agents.refresh();
    Trio.agents.renderPage(panel);
    text = panel.textContent || '';
    check(`once the roster loads, the empty state is about the FILTER again `
          + `(${text.slice(0, 60).trim()})`,
          /No agents match/i.test(text) && !/managed agents are off/i.test(text));
  } finally {
    Trio.api.get = realGet;
  }
}

console.log('\n' + passed + ' passed, ' + failures.length + ' failed');
if (failures.length) failures.forEach(f => console.log('  ✗ ' + f));
process.exit(failures.length ? 1 : 0);
})();
