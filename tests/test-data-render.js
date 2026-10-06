'use strict';

// Headless render coverage for the Data page (js/46-data.js → Trio.data).
// Loads the real shipped bundle in the dependency-free DOM harness, stubs the
// /api/storage response, renders into a fake panel, and asserts the structure.
const assert = require('assert');
const { load } = require('./dom-harness');
const cx = load();
const Trio = cx.hooks.Trio;
let failures = 0;
function check(name, fn) {
  try { fn(); console.log('PASS: ' + name); }
  catch (e) { failures++; console.log('FAIL: ' + name + ' — ' + e.message); }
}

check('Trio.data is registered by the bundle', () => {
  assert.ok(Trio.data && typeof Trio.data.renderPage === 'function');
});

check('fmtBytes formats across units', () => {
  const f = Trio.data.fmtBytes;
  assert.strictEqual(f(0), '0 B');
  assert.strictEqual(f(512), '512 B');
  assert.strictEqual(f(1024), '1.0 KB');
  assert.strictEqual(f(1536), '1.5 KB');
  assert.strictEqual(f(2 * 1024 * 1024), '2.0 MB');
});

const SAMPLE = {
  ok: true,
  db_bytes: 2 * 1024 * 1024,
  db_reclaimable_bytes: 512 * 1024,
  attachments: { count: 3, bytes: 1500 },
  by_channel: [
    { channel: 'keepchan', message_count: 4, est_message_bytes: 900,
      attachment_count: 2, attachment_bytes: 1300, archived: false },
    { channel: 'nth-agent-inbox', message_count: 10, est_message_bytes: 5000,
      attachment_count: 1, attachment_bytes: 200, archived: false },
    { channel: 'oldchan', message_count: 3, est_message_bytes: 300,
      attachment_count: 0, attachment_bytes: 0, archived: true },
  ],
};

(async () => {
  // Stub the workspace-global storage fetch (channelScoped must be false).
  let seenPath = null, seenScoped = null;
  Trio.api.get = async (path, scoped) => { seenPath = path; seenScoped = scoped; return SAMPLE; };

  const panel = cx.document.createElement('section');
  await Trio.data.renderPage(panel);

  check('renderPage calls GET /api/storage workspace-global', () => {
    assert.strictEqual(seenPath, '/api/storage');
    assert.strictEqual(seenScoped, false);
  });
  check('renders the Data page header', () => {
    const head = panel.querySelector('.page-head');
    assert.ok(head && head.textContent.startsWith('Data'));
  });
  check('renders three overview cards', () => {
    assert.strictEqual(panel.querySelectorAll('.data-card').length, 3);
  });
  check('renders three prune controls', () => {
    assert.strictEqual(panel.querySelectorAll('.data-prune-row').length, 3);
  });
  check('renders one table row per channel', () => {
    assert.strictEqual(panel.querySelectorAll('.dt-row').length, 3);
  });
  check('archived channel shows a badge; active ones do not', () => {
    const badges = panel.querySelectorAll('.data-badge');
    assert.strictEqual(badges.length, 1);
    assert.strictEqual(badges[0].textContent, 'archived');
  });
  check('the agent inbox row has no delete button; others do', () => {
    const rows = panel.querySelectorAll('.dt-row');
    let inboxDelete = null, inboxSys = null, otherDeletes = 0;
    rows.forEach(tr => {
      const label = tr.querySelector('.dt-chan').textContent;
      const del = tr.querySelector('.dp-btn');
      if (label === 'nth-agent-inbox') { inboxDelete = del; inboxSys = tr.querySelector('.dt-sys'); }
      else if (del) otherDeletes++;
    });
    assert.strictEqual(inboxDelete, null, 'inbox must not be deletable');
    assert.ok(inboxSys, 'inbox row shows a "system" hint instead of a blank cell');
    assert.strictEqual(otherDeletes, 2, 'other channels get a delete button');
  });
  check('delete buttons carry a channel-specific aria-label', () => {
    const del = panel.querySelector('.dp-btn.sm');
    assert.ok(del && /^Delete channel /.test(del.getAttribute('aria-label') || ''));
  });
  check('the message-size column is flagged as an estimate', () => {
    const table = panel.querySelector('.data-table');
    assert.ok(table && /est\./.test(table.textContent));
  });

  // Error path: a failing storage fetch renders a message, not a crash.
  Trio.api.get = async () => { throw new Error('boom'); };
  const panel2 = cx.document.createElement('section');
  await Trio.data.renderPage(panel2);
  check('storage load failure renders an inline error', () => {
    const msg = panel2.querySelector('.home-empty');
    assert.ok(msg && /Could not load storage/.test(msg.textContent));
  });

  console.log(`\n${failures} failure(s)`);
  process.exit(failures ? 1 : 0);
})();
