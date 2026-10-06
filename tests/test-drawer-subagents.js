'use strict';
// Coverage for #6 — recently-spawned subagents in the channel-details member
// list. detailMember() adds an async-filled placeholder for AGENT members only;
// renderSubagentList() formats the "recent subagents" rows (type + time-ago),
// capped, escaped. Both are pure string builders, tested in a minimal sandbox.
const fs = require('fs');
const vm = require('vm');
const path = require('path');
// Resolved against THIS FILE, not the working directory: tests/run-all.sh
// runs from tests/, and a cwd-relative path made these pass standalone
// from the repo root while failing under the runner.
const WEB_JS = n => path.resolve(__dirname, '..', 'server', 'web', 'js', n);

function baseContext() {
  const context = {
    window: {}, location: { search: '', pathname: '/', href: 'http://localhost/' },
    URLSearchParams,
    document: {
      getElementById: () => null, querySelector: () => null,
      querySelectorAll: () => [], createElement: () => ({ classList: { add() {}, remove() {} }, style: {}, append() {}, setAttribute() {} }),
      body: { classList: { toggle() {} }, append() {} }, documentElement: { dataset: {} },
    },
    localStorage: { getItem: () => null, setItem() {} },
    fetch: () => Promise.resolve({ ok: false, status: 404 }),
    EventTarget: class { addEventListener() {} dispatchEvent() {} }, CustomEvent: class {},
    console, setInterval() {}, Date,
  };
  context.window = context; context.globalThis = context;
  vm.createContext(context); return context;
}
const cx = baseContext();
['01-store.js', '02-api.js', '05-loader.js', '04-events.js', '06-core.js', '09-ui.js', '20-workspace.js']
  .forEach(m => vm.runInContext(fs.readFileSync(WEB_JS(m), 'utf8'), cx));

let failures = 0;
function check(name, cond) { console.log((cond ? 'PASS: ' : 'FAIL: ') + name); if (!cond) failures++; }
const { Trio } = cx;
const { detailMember, renderSubagentList, subagentsFromResponse } = Trio.workspace;

// --- HTTP response contract -----------------------------------------------
const apiItems = [{ id: 7, target: 'sauron', tool_name: 'Agent', created_at: new Date().toISOString() }];
check('extracts the /api/tools subagents array',
  subagentsFromResponse({ ok: true, member_id: 'ag_1', count: 1, subagents: apiItems }) === apiItems);
check('malformed or missing API data degrades to an empty list',
  subagentsFromResponse(null).length === 0 && subagentsFromResponse({ subagents: {} }).length === 0);

// --- detailMember: placeholder only for agents ---------------------------
Trio.state.operator = { id: 'op1', name: 'jd' };
check('agent member gets a subagents placeholder',
  /data-subagents-for="ag_1"/.test(detailMember({ id: 'ag_1', name: 'Nova', kind: 'agent' })));
check('a member with no explicit kind is treated as an agent',
  /data-subagents-for="ag_2"/.test(detailMember({ id: 'ag_2', name: 'Ada' })));
check('a human member gets no placeholder',
  !/data-subagents-for/.test(detailMember({ id: 'h1', name: 'guest', kind: 'human' })));
check('the operator (by id, no kind) gets no placeholder',
  !/data-subagents-for/.test(detailMember({ id: 'op1', name: 'jd' })));

// --- renderSubagentList: formatting, cap, escaping -----------------------
check('empty / null subagents render nothing', renderSubagentList([]) === '' && renderSubagentList(null) === '');
const iso = new Date(Date.now() - 5 * 60000).toISOString();
const one = renderSubagentList([{ target: 'sauron', tool_name: 'Agent', created_at: iso }]);
check('renders the subagent type', /sauron/.test(one));
check('renders a non-empty time-ago', /class="sa-time">[^<]+</.test(one));
check('renders a "Recent subagents" header and arrow', /Recent subagents/.test(one) && /↳/.test(one));
check('falls back to tool_name when target is empty', /Agent/.test(renderSubagentList([{ target: '', tool_name: 'Agent', created_at: iso }])));
const many = renderSubagentList(Array.from({ length: 6 }, (_, i) => ({ target: 'sub' + i, created_at: iso })));
check('caps the visible rows at 4', (many.match(/subagent-row/g) || []).length === 4);
check('notes the count of earlier spawns', /\+2 earlier/.test(many));
check('escapes the subagent target (no raw HTML)', /&lt;img&gt;/.test(renderSubagentList([{ target: '<img>', created_at: iso }])));
const bad = renderSubagentList([{ target: 'x', created_at: 'not-a-date' }]);
check('a malformed timestamp falls back to "now", never "NaN"', /sa-time">now</.test(bad) && !/NaN/.test(bad));

console.log((failures ? 'FAILED' : 'OK') + ' — ' + failures + ' failure(s)');
process.exit(failures ? 1 : 0);
