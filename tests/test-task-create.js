'use strict';
// Focused coverage for manual task creation (openTaskModal): the Tasks-view
// "New task" action opens a modal (task description + channel picker) and, on
// submit, POSTs "$task <desc>" to /api/send scoped to the CHOSEN channel — the
// same server flow as trio_send(task=True). Runs in a minimal vm sandbox with
// a permissive fake DOM (showView touches the DOM after the POST; the POST is
// captured before that, so the fake DOM only needs to not throw).
const fs = require('fs');
const vm = require('vm');
const path = require('path');
// Resolved against THIS FILE, not the working directory: tests/run-all.sh
// runs from tests/, and a cwd-relative path made these pass standalone
// from the repo root while failing under the runner.
const WEB_JS = n => path.resolve(__dirname, '..', 'server', 'web', 'js', n);

function mkEl() {
  const el = {
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    style: {}, dataset: {}, hidden: false, _fields: {},
    append() {}, appendChild() {}, prepend() {}, replaceChildren() {}, remove() {},
    setAttribute() {}, removeAttribute() {}, addEventListener() {}, removeEventListener() {},
    querySelector() { return mkEl(); }, querySelectorAll() { return []; },
    set innerHTML(_v) {}, get innerHTML() { return ''; },
    textContent: '', value: '', showModal() {}, close() {},
    focus() { el._focused = true; },
  };
  return el;
}
function baseContext() {
  const context = {
    window: {}, location: { search: '', pathname: '/', href: 'http://localhost/' },
    URLSearchParams,
    document: {
      getElementById: () => mkEl(), querySelector: () => mkEl(),
      querySelectorAll: () => [], createElement: () => mkEl(),
      createDocumentFragment: () => mkEl(),
      body: mkEl(), documentElement: { dataset: {} },
    },
    localStorage: { getItem: () => null, setItem() {} },
    fetch: () => Promise.resolve({ ok: false, status: 404 }),
    EventTarget: class { addEventListener() {} dispatchEvent() {} removeEventListener() {} },
    CustomEvent: class {}, console, setInterval() {}, clearInterval() {}, setTimeout() {},
    FormData: class { constructor(form) { this._f = (form && form._fields) || {}; } get(n) { return n in this._f ? this._f[n] : null; } },
  };
  context.window = context; context.globalThis = context;
  vm.createContext(context); return context;
}
function loadModule(name, ctx) { vm.runInContext(fs.readFileSync(WEB_JS(name), 'utf8'), ctx); }

const cx = baseContext();
['01-store.js', '02-api.js', '05-loader.js', '04-events.js', '00-core.js', '06-ui.js', '20-workspace.js'].forEach(m => loadModule(m, cx));
// Shared focus-spy element for the task-title input so we can assert autofocus.
const taskFocusEl = mkEl();
cx.document.getElementById = (id) => (id === 'new-task-title' ? taskFocusEl : mkEl());

let failures = 0;
function check(name, cond) { console.log((cond ? 'PASS: ' : 'FAIL: ') + name); if (!cond) failures++; }
const fakeNode = fields => ({ querySelector: () => ({ _fields: fields }) });

const { Trio } = cx;
const openTaskModal = Trio.workspace.openTaskModal;

// Capture modal + toast + api.
const opened = []; const toasts = [];
Trio.ui.modal = (title, body, submit) => { opened.push({ title, body, submit }); };
Trio.ui.toast = m => toasts.push(m);
let postArgs = null;
Trio.api.post = (path, body) => { postArgs = { path, body }; return Promise.resolve({ ok: true }); };
Trio.api.get = () => Promise.resolve({ tasks: [] });

(async () => {
  // --- No channels: guides the operator, does not open an empty modal.
  Trio.state.channels = [];
  opened.length = 0; toasts.length = 0;
  openTaskModal();
  check('no channels: does not open a modal', opened.length === 0);
  check('no channels: toasts a hint to create a channel first', toasts.length === 1 && /channel/i.test(toasts[0]));

  // --- With channels: modal offers a channel picker.
  Trio.state.channels = [
    { code: 'general', topic: 'x' },
    { code: 'design', topic: 'y' },
    { code: 'old', topic: 'z', archived: true },
  ];
  Trio.state.channel = 'design'; // current conversation → default selection
  opened.length = 0; toasts.length = 0;
  openTaskModal();
  check('opens a modal titled "New task"', opened.length === 1 && opened[0].title === 'New task');
  const body = opened[0].body || '';
  check('modal has a task description input', /name="title"/.test(body) && /required/.test(body));
  check('modal has a channel <select>', /<select name="channel">/.test(body));
  check('picker lists non-archived channels', /value="general"/.test(body) && /value="design"/.test(body));
  check('picker excludes archived channels', !/value="old"/.test(body));
  check('picker defaults to the current channel', /value="design" selected/.test(body));
  check('description input caps length (maxlength)', /maxlength="\d+"/.test(body));
  check('opening focuses the description, not the × button', taskFocusEl._focused === true);

  // --- Submit: POST "$task <desc>" to /api/send scoped to the CHOSEN channel.
  postArgs = null;
  try { await opened[0].submit(fakeNode({ title: '  Ship the empty states  ', channel: 'general' })); } catch (_) { /* showView DOM is best-effort here */ }
  check('submit posts to /api/send scoped to the chosen channel',
    !!postArgs && postArgs.path === '/api/send?channel=general');
  check('submit sends a trimmed "$task <desc>" body',
    !!postArgs && postArgs.body && postArgs.body.content === '$task Ship the empty states');

  // --- Empty description: re-prompts instead of posting a blank task.
  opened.length = 0; toasts.length = 0; postArgs = null;
  openTaskModal();
  try { await opened[0].submit(fakeNode({ title: '   ', channel: 'general' })); } catch (_) {}
  check('empty description does not POST', postArgs === null);
  check('empty description reopens the modal', opened.length === 2);

  // --- Server error: reopen pre-filled so the operator doesn't lose their text.
  Trio.api.post = () => Promise.reject(new Error('503 busy'));
  opened.length = 0; toasts.length = 0;
  openTaskModal();
  try { await opened[0].submit(fakeNode({ title: 'Fix the thing', channel: 'design' })); } catch (_) {}
  check('server error reopens the modal', opened.length === 2);
  check('server error preserves the typed description', /value="Fix the thing"/.test(opened[1].body || ''));
  check('server error surfaces the error as a toast', toasts.some(t => /503/.test(t)));

  console.log((failures ? 'FAILED' : 'OK') + ' — ' + failures + ' failure(s)');
  process.exit(failures ? 1 : 0);
})().catch(err => { console.log('FAIL: task-create test threw — ' + err.stack); process.exit(1); });
