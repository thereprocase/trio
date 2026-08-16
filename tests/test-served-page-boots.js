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

// The body must arrive with a Content-Length. The server reads that header to
// size the body and answers "missing or oversized body" without it — node's
// http.request defaults to chunked encoding, which this stdlib server does not
// accept. Omitting it made EVERY client POST fail, silently, including the
// mark-read calls the read-path checks were already making.
// FormData is normalised through Response so multipart framing and the
// boundary in Content-Type come from the platform rather than by hand.
async function bodyToBuffer(body) {
  if (body == null) return { buf: null, headers: {} };
  if (typeof body === 'string') return { buf: Buffer.from(body), headers: {} };
  if (typeof FormData !== 'undefined' && body instanceof FormData) {
    const res = new Response(body);
    const buf = Buffer.from(await res.arrayBuffer());
    return { buf, headers: { 'Content-Type': res.headers.get('content-type') } };
  }
  // A Blob/File is what the upload path sends: /api/upload takes the raw image
  // bytes as the body, not a multipart part. Content-Type is the image's own
  // mime and is set by the caller, so it is not overridden here.
  if (typeof Blob !== 'undefined' && body instanceof Blob) {
    return { buf: Buffer.from(await body.arrayBuffer()), headers: {} };
  }
  return { buf: Buffer.from(body), headers: {} };
}

async function request(base, url, init = {}) {
  const u = new URL(url.startsWith('http') ? url : base + url);
  const { buf, headers: bodyHeaders } = await bodyToBuffer(init.body);
  const headers = { ...(init.headers || {}), ...bodyHeaders };
  if (buf) headers['Content-Length'] = String(buf.length);
  return new Promise((resolve, reject) => {
    const req = http.request({
      agent, hostname: u.hostname, port: u.port, path: u.pathname + u.search,
      method: init.method || 'GET', headers,
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
    if (buf) req.write(buf);
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

// Ask the OS for a free port rather than deriving one from the pid. The pid
// form (8700 + pid % 900) collides whenever two runs draw congruent pids, and
// it collides with anything already on that port — including a server this
// suite itself left behind. The failure mode is bad: the health check times
// out and the test reports a clean, confusing FAIL that passes on the next
// run. Observed exactly that under run-all.sh.
function freePort() {
  return new Promise((resolve, reject) => {
    const probe = require('net').createServer();
    probe.on('error', reject);
    probe.listen(0, '127.0.0.1', () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });
}
let PORT, BASE;
const bootstrapFor = (port) => `
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
sys.argv = ["nth_web.py", "served", "--port", ${JSON.stringify(String(port))}]
web.main()
`;

let child;

// Clean up on signals too, not only on the normal exit path. Without this a
// Ctrl-C or a CI timeout that SIGKILLs the runner orphans the python child —
// which is still holding a port — and leaks the scratch DB directory.
function cleanup() {
  if (child) { try { child.kill('SIGKILL'); } catch (e) { /* already gone */ } }
  try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (e) { /* gone */ }
}
for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
  process.on(sig, () => { cleanup(); process.exit(130); });
}
process.on('exit', cleanup);

(async () => {
  PORT = await freePort();
  BASE = `http://127.0.0.1:${PORT}`;
  child = spawn('python3', ['-c', bootstrapFor(PORT)], { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'] });
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

  // ── WRITE PATHS ─────────────────────────────────────────────────────────
  // Everything above is a read. Reads alone are what let the upload seam ship:
  // /api/upload returned no `url`, the composer did apiUrl(attachment.url),
  // and the resulting TypeError was swallowed by a catch that had already
  // spliced the image out — so attaching an image was completely broken while
  // every read-path test stayed green. These drive the client's OWN request
  // builders against the real server, which is the only place a request/response
  // disagreement can surface.

  // POST /api/send, through Trio.api like the composer does.
  let sendResult = null, sendErr = null;
  try { sendResult = await Trio.api.post('/api/send', { content: 'written by the smoke test' }); }
  catch (e) { sendErr = e; }
  check('POST /api/send succeeds through the client api layer',
        !sendErr && sendResult && sendResult.ok !== false, sendErr && sendErr.message);
  const after = await (await request(BASE, '/api/mentions?channel=served')).json();
  check('  the server accepted it (endpoint still answers 200)', !!after);

  // /api/upload, through the client's own uploader — which is what the
  // composer calls, so this is the real path. Node has Blob/File globally; the
  // sandbox does not, so lend them in.
  sandbox.Blob = Blob; sandbox.File = File;
  // A one-pixel PNG: the server sniffs magic bytes, so random bytes are rejected
  // for the wrong reason and would make this test pass on a broken client.
  const PNG = Buffer.from(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
    'base64');
  let uploaded = null, uploadErr = null;
  try {
    uploaded = await Trio.api.upload(new File([PNG], 'dot.png', { type: 'image/png' }));
  } catch (e) { uploadErr = e; }
  check('POST /api/upload succeeds through the client uploader',
        !uploadErr && uploaded && uploaded.ok !== false, uploadErr && uploadErr.message);
  if (uploaded) {
    check('  the response carries an id', Number.isInteger(uploaded.id));
    // THE SEAM. The composer does exactly this with the response, and an absent
    // url throws inside its try — discarding the upload the user just made.
    check('  the response carries a url — without it the composer throws and ' +
          'silently discards the image',
          typeof uploaded.url === 'string' && uploaded.url.length > 0);
    let apiUrlErr = null, built = null;
    try { built = Trio.api.url(uploaded.url); } catch (e) { apiUrlErr = e; }
    check('  api.url() accepts it without throwing (the composer\'s exact call)',
          !apiUrlErr && typeof built === 'string', apiUrlErr && apiUrlErr.message);
    if (built) {
      const fetched = await request(BASE, built);
      check(`  and the built URL actually serves the image (${fetched.status})`,
            fetched.status === 200);
    }
  }

  console.log('\n' + passed + ' passed, ' + failures.length + ' failed');
  if (failures.length) failures.forEach(f => console.log('  ✗ ' + f));
})().catch(e => {
  console.log('FAIL: the smoke test itself threw — ' + e.stack);
  failures.push('harness');
}).finally(() => {
  cleanup();
  process.exit(failures.length ? 1 : 0);
});
