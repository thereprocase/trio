// The page the server ACTUALLY SERVES must boot, against a REAL server.
//
// Every other client test loads the source modules from server/web/js/ with
// fetch stubbed. That is the right shape for testing behaviour, and it is
// structurally blind to three things:
//
//   * COMPOSITION — the modules are inlined into one document in a declared
//     order, with substitutions applied. A module ordered wrongly, or a
//     substitution that silently no-ops, produces a page that fails only in
//     a browser.
//   * PAYLOAD AGREEMENT — the stubs return whatever the test author wrote. If
//     the server's actual JSON disagrees with what the client reads, no stub
//     will ever notice. This is exactly how the roster regression shipped: the
//     client filtered roster events on a `channel` field the server did not
//     send, so the member list was silently never populated.
//   * BOOT ITSELF — Trio.boot() fetches /api/meta and mounts every feature.
//
// So this starts a real nth_web server on a scratch database, fetches "/",
// evaluates the served script blocks, boots, reads the real SSE stream through
// the client's own dispatcher, and renders. It is slower than the unit tests
// and deliberately shallow: it asks "does the whole thing come up and show the
// messages", not "is each behaviour correct".
//
// Usage: node tests/test-served-page-boots.js
'use strict';

const assert = require('assert');
const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');
const vm = require('vm');
const { spawn } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const failures = [];
let passed = 0;
function check(name, cond, detail = '') {
  if (cond) { passed++; console.log('PASS: ' + name); }
  else { failures.push(name); console.log('FAIL: ' + name + (detail ? ' — ' + detail : '')); }
}

// The server speaks HTTP/1.0 and closes every connection. Node's global fetch
// pools and reuses sockets, so it races that close and reports ECONNRESET on
// perfectly good requests — an artifact of the client, not of either side; a
// browser honours Connection: close. Use an agent that never reuses a socket.
const agent = new http.Agent({ keepAlive: false, maxSockets: 8 });
function request(base, url, init = {}) {
  const u = new URL(url.startsWith('http') ? url : base + url);
  return new Promise((resolve, reject) => {
    const req = http.request({
      agent, hostname: u.hostname, port: u.port, path: u.pathname + u.search,
      method: init.method || 'GET', headers: init.headers || {},
    }, res => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', c => { body += c; });
      res.on('end', () => resolve({
        ok: res.statusCode >= 200 && res.statusCode < 300,
        status: res.statusCode,
        text: async () => body,
        json: async () => JSON.parse(body),
      }));
    });
    req.on('error', reject);
    if (init.body) req.write(init.body);
    req.end();
  });
}

function readStream(base, url, ms) {
  return new Promise(resolve => {
    const u = new URL(base + url);
    const frames = [];
    const req = http.request({
      agent, hostname: u.hostname, port: u.port, path: u.pathname + u.search,
    }, res => {
      let buf = '';
      res.setEncoding('utf8');
      res.on('data', c => {
        buf += c;
        let i;
        while ((i = buf.indexOf('\n\n')) >= 0) {
          const frame = buf.slice(0, i); buf = buf.slice(i + 2);
          for (const line of frame.split('\n')) {
            if (line.startsWith('data:')) frames.push(line.slice(5).trim());
          }
        }
      });
      setTimeout(() => { req.destroy(); resolve(frames); }, ms);
    });
    req.on('error', () => resolve(frames));
    req.end();
  });
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'nth_served_'));
const PORT = 8700 + (process.pid % 900);
const BASE = `http://127.0.0.1:${PORT}`;
const BOOTSTRAP = `
import sys, pathlib, json
sys.path.insert(0, ${JSON.stringify(path.join(ROOT, 'server'))})
import nth_server as srv
srv.DB_DIR = pathlib.Path(${JSON.stringify(tmp)}); srv.DB_PATH = srv.DB_DIR / "nth.db"
r = json.loads(srv.nth_connect(summary='served-page smoke', name='Ada', channel='served'))
ch, ada = r['channel'], r['member_id']
bo = json.loads(srv.nth_connect(summary='peer', name='Bo', channel=ch))['member_id']
srv.nth_send(channel=ch, member_id=ada, message='First message in the served page.')
srv.nth_send(channel=ch, member_id=bo, message='@Ada please look at this')
import nth_web as web
web.DB_PATH = srv.DB_PATH
sys.argv = ["nth_web.py", "served", "--port", ${JSON.stringify(String(PORT))}]
web.main()
`;

let child;
(async () => {
  child = spawn('python3', ['-c', BOOTSTRAP], { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'] });
  let serverLog = '';
  child.stdout.on('data', d => { serverLog += d; });
  child.stderr.on('data', d => { serverLog += d; });

  // Wait for it to answer rather than sleeping a fixed amount.
  let up = false;
  for (let i = 0; i < 40 && !up; i++) {
    await sleep(250);
    try { up = (await request(BASE, '/api/health')).ok; } catch (e) { /* not yet */ }
  }
  check('a real nth_web server started on a scratch database', up,
        serverLog.split('\n').slice(-3).join(' | '));
  if (!up) return;

  const page = await (await request(BASE, '/')).text();
  check('GET / returns a complete document',
        page.startsWith('<!doctype html>') && page.trimEnd().endsWith('</html>'));

  const blocks = [...page.matchAll(
    /<script(?: data-trio-source="([^"]*)")?[^>]*>([\s\S]*?)<\/script>/g)];
  check(`the page carries its script blocks (${blocks.length})`, blocks.length > 1);

  // Borrow the harness's DOM, then wipe the registry so the SERVED bundle
  // initialises from scratch rather than the source modules load() ran.
  const harness = require('./dom-harness');
  const sandbox = harness.load().window;
  sandbox.fetch = (url, init) => request(BASE, url, init);
  sandbox.EventSource = function () { this.close = () => {}; };
  delete sandbox.Trio;
  sandbox.window.Trio = undefined;

  const context = vm.createContext(sandbox);
  const scriptErrors = [];
  for (const [, src, body] of blocks) {
    try { vm.runInContext(body, context, { filename: src || 'index.html:inline', timeout: 15000 }); }
    catch (e) { scriptErrors.push(`${src || 'inline'}: ${e.message}`); }
  }
  check('every served script block evaluates without throwing',
        !scriptErrors.length, scriptErrors.join('; '));

  const Trio = sandbox.window.Trio;
  check('the served bundle created the window.Trio namespace', !!Trio);
  if (!Trio) return;
  for (const ns of ['store', 'api', 'router', 'markdown', 'conversation',
                    'composer', 'workspace', 'agents', 'preferences',
                    'lifecycle', 'fileLinks', 'sidebar', 'boot']) {
    check(`  Trio.${ns} is registered`, !!Trio[ns]);
  }

  let bootErr = null;
  try {
    await Trio.boot(() => {
      for (const n of ['conversation', 'workspace', 'agents', 'preferences', 'router', 'composer']) {
        try { Trio.lifecycle.mount(n, Trio[n]); } catch (e) { scriptErrors.push(`mount ${n}: ${e.message}`); }
      }
    });
  } catch (e) { bootErr = e; }
  check('Trio.boot() completes against the live server', !bootErr,
        bootErr && bootErr.message);
  check('boot resolved an operator identity from /api/meta',
        !!(Trio.state.operator && Trio.state.operator.id));

  // The deep-link paths the client pushes must all serve the shell, or a
  // reload of any non-channel view 404s.
  for (const p of ['/inbox', '/tasks', '/agents', '/settings', '/archive', '/data']) {
    const res = await request(BASE, p);
    check(`  ${p} serves the app shell`, res.status === 200);
  }
  check('  an unknown path still 404s',
        (await request(BASE, '/no-such-page')).status === 404);

  // Messages arrive on the SSE stream, not from /api/meta. Read the real
  // stream and hand each frame to the client's own dispatcher — the exact
  // path a browser takes.
  Trio.state.channel = 'served';
  const frames = await readStream(BASE, '/api/events?channel=served', 2500);
  check(`the SSE stream delivered frames (${frames.length})`, frames.length > 0);
  for (const f of frames) {
    try {
      const payload = JSON.parse(f);
      const list = Array.isArray(payload) ? payload
                 : Array.isArray(payload.messages) ? payload.messages : [payload];
      list.forEach(p => Trio.dispatchSSEEvent(p));
    } catch (e) { /* heartbeat comment or non-JSON */ }
  }
  check(`the client ingested the history (${Trio.state.messages.size} messages)`,
        Trio.state.messages.size >= 3);
  // The roster is the regression this file was written for: it is applied only
  // if the event names its channel, and when it is not applied NOTHING errors —
  // member names simply never resolve.
  check(`the roster was applied (${Trio.state.members.size} members) — if this ` +
        'is 0 the roster event is missing its channel field',
        Trio.state.members.size >= 2);

  Trio.conversation.render();
  const list = sandbox.document.getElementById('messages');
  const html = list.innerHTML || '';
  check('the conversation rendered message cards', list.children.length > 0);
  check('  message text reached the page', html.includes('served page'));
  // Query the DOM for class-based probes: dom-harness sets className as a
  // plain property and its serializer only emits _attrs, so `class` never
  // appears in innerHTML even when the element carries it.
  check('  an @mention resolved to a member NAME, not a raw id',
        [...list.querySelectorAll('.inline-mention')].some(e => /@Ada/.test(e.textContent)));

  console.log('\n' + passed + ' passed, ' + failures.length + ' failed');
  if (failures.length) failures.forEach(f => console.log('  ✗ ' + f));
})().catch(e => {
  console.log('FAIL: the smoke test itself threw — ' + e.stack);
  failures.push('harness');
}).finally(() => {
  if (child) { try { child.kill('SIGKILL'); } catch (e) { /* already gone */ } }
  fs.rmSync(tmp, { recursive: true, force: true });
  process.exit(failures.length ? 1 : 0);
});
