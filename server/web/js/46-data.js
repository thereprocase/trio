(() => {
  'use strict';
  // Data page — storage overview + prune controls. Rendered into the account
  // menu's "Data" view via the showView('data') seam in 20-workspace.js, which
  // calls Trio.data.renderPage(panel). Backend: GET /api/storage, POST /api/prune
  // (both workspace-global, so every api call passes channelScoped=false).
  const Trio = window.Trio;
  if (!Trio) throw new Error('Data page requires Trio core');
  const { api, ui } = Trio;
  const INBOX = 'nth-agent-inbox';  // never deletable (backend also rejects it)

  const esc = value => String(value ?? '').replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + ' B';
    const units = ['KB', 'MB', 'GB', 'TB'];
    let i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
    return (n < 10 ? n.toFixed(1) : Math.round(n)) + ' ' + units[i];
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function plural(n, word) { return `${n} ${word}${n === 1 ? '' : 's'}`; }

  // ── Prune flow: always dry-run first, show the real preview inside a confirm
  // modal (with an irreversibility warning), then execute on confirm. buildConfirm
  // turns the dry-run response into {message, description}, or null to abort.
  // Announce a result to both the toast host and the aria-live region so
  // assistive tech hears destructive-action outcomes (Frodo).
  function announce(msg) { ui.toast(msg); ui.setLive?.(msg); }

  // Disable a button while an async action is in flight so a slow dry-run (it
  // stats files on disk) can't be double-clicked into a stacked confirm (Frodo).
  async function withPending(btn, fn) {
    if (btn) { btn.disabled = true; btn.classList.add('pending'); }
    try { return await fn(); }
    finally { if (btn) { btn.disabled = false; btn.classList.remove('pending'); } }
  }

  async function previewThenConfirm(panel, body, buildConfirm, emptyMsg) {
    let preview;
    try {
      preview = await api.post('/api/prune', { ...body, dry_run: true }, false);
    } catch (e) { announce(e.message || 'Preview failed'); return; }
    const confirmParts = buildConfirm(preview);
    if (!confirmParts) { announce(emptyMsg || 'Nothing to prune.'); return; }
    ui.confirmAction(confirmParts.message, confirmParts.description, async () => {
      try {
        const res = await api.post('/api/prune', { ...body, dry_run: false }, false);
        const fb = res.freed_bytes || {};
        const parts = [`Done — freed ${fmtBytes((fb.attachments || 0) + (fb.db || 0))}`];
        if (res.file_errors) parts.push(`${plural(res.file_errors, 'file')} could not be deleted`);
        if (res.vacuum_deferred) parts.push('disk will be reclaimed on the next run');
        announce(parts.join(' · ') + '.');
        renderPage(panel);
      } catch (e) { announce(e.message || 'Prune failed'); }
    });
  }

  function readDays(input) {
    const v = parseInt(input.value, 10);
    if (!Number.isFinite(v) || v < 0) { announce('Enter a whole number of days (0 or more).'); return null; }
    return v;
  }

  // Phrase an age-scoped confirm; 0 days deletes EVERYTHING regardless of age,
  // which "older than 0 days" hides — so 0 gets its own explicit wording (Frodo).
  function agePhrase(days, noun) {
    return days === 0 ? `ALL ${noun} regardless of age` : `${noun} older than ${days} days`;
  }

  // ── Overview cards ──────────────────────────────────────────────────────────
  function overviewCards(data) {
    const wrap = el('div', 'data-cards');
    const card = (label, value, sub) => {
      const c = el('div', 'data-card');
      c.append(el('span', 'dc-label', label));
      c.append(el('span', 'dc-value', value));
      if (sub) c.append(el('span', 'dc-sub', sub));
      return c;
    };
    const reclaimable = data.db_reclaimable_bytes || 0;
    wrap.append(card('Database', fmtBytes(data.db_bytes),
      reclaimable ? `~${fmtBytes(reclaimable)} reclaimable` : 'fully packed'));
    const att = data.attachments || { count: 0, bytes: 0 };
    wrap.append(card('Attachments', fmtBytes(att.bytes), plural(att.count, 'file')));
    const chans = (data.by_channel || []).length;
    const archived = (data.by_channel || []).filter(c => c.archived).length;
    wrap.append(card('Channels', String(chans),
      archived ? `${archived} archived` : 'none archived'));
    return wrap;
  }

  // ── Prune controls ──────────────────────────────────────────────────────────
  function pruneControls(panel) {
    const section = el('section', 'data-section');
    section.append(el('h3', null, 'Prune'));

    // 1. Old attachment files (all channels).
    const r1 = el('div', 'data-prune-row');
    const d1 = el('div', 'dp-text');
    d1.append(el('span', 'dp-title', 'Delete old attachment files'));
    d1.append(el('span', 'dp-desc', 'Remove uploaded images/files older than the given age, across every channel.'));
    r1.append(d1);
    const ctl1 = el('div', 'dp-ctl');
    const days1 = daysInput(30, 'Age in days for old attachments');
    ctl1.append(days1.wrap);
    const b1 = el('button', 'dp-btn danger', 'Preview & delete');
    b1.type = 'button';
    b1.addEventListener('click', () => {
      const days = readDays(days1.input); if (days == null) return;
      withPending(b1, () => previewThenConfirm(panel, { action: 'prune_attachments', older_than_days: days },
        p => {
          const n = p.counts?.attachments || 0;
          if (!n) return null;
          return {
            message: `Delete ${plural(n, 'attachment')} — ${agePhrase(days, 'files')}?`,
            description: `Frees ~${fmtBytes(p.would_free_bytes?.attachments || 0)} of disk. This cannot be undone.`,
          };
        }));
    });
    ctl1.append(b1);
    r1.append(ctl1);
    section.append(r1);

    // 2. Messages in archived channels.
    const r2 = el('div', 'data-prune-row');
    const d2 = el('div', 'dp-text');
    d2.append(el('span', 'dp-title', 'Delete messages in archived channels'));
    d2.append(el('span', 'dp-desc', 'Only archived channels are touched — active channels are never affected.'));
    r2.append(d2);
    const ctl2 = el('div', 'dp-ctl');
    const days2 = daysInput(30, 'Age in days for archived-channel messages');
    ctl2.append(days2.wrap);
    const b2 = el('button', 'dp-btn danger', 'Preview & delete');
    b2.type = 'button';
    b2.addEventListener('click', () => {
      const days = readDays(days2.input); if (days == null) return;
      withPending(b2, () => previewThenConfirm(panel, { action: 'prune_archived_messages', older_than_days: days },
        p => {
          const m = p.counts?.messages || 0;
          const a = p.counts?.attachments || 0;
          if (!m && !a) return null;
          const bits = [plural(m, 'message')];
          if (a) bits.push(plural(a, 'attachment'));
          return {
            message: `Delete ${bits.join(' + ')} — ${agePhrase(days, 'messages in archived channels')}?`,
            description: `Frees ~${fmtBytes(p.would_free_bytes?.attachments || 0)} of files (plus reclaimed database space). This cannot be undone.`,
          };
        }));
    });
    ctl2.append(b2);
    r2.append(ctl2);
    section.append(r2);

    // 3. Reclaim space (VACUUM) — non-destructive; rewrites the DB compactly.
    const r3 = el('div', 'data-prune-row');
    const d3 = el('div', 'dp-text');
    d3.append(el('span', 'dp-title', 'Reclaim space'));
    d3.append(el('span', 'dp-desc', 'Rewrite the database to return freed pages to disk (VACUUM). Safe — no data is deleted.'));
    r3.append(d3);
    const ctl3 = el('div', 'dp-ctl');
    const b3 = el('button', 'dp-btn', 'Reclaim');
    b3.type = 'button';
    b3.addEventListener('click', () => {
      withPending(b3, () => previewThenConfirm(panel, { action: 'reclaim' },
        p => {
          const db = p.would_free_bytes?.db || 0;
          if (!db) return null;  // already compact — don't run a pointless VACUUM
          return {
            message: 'Reclaim database space now?',
            description: `Compacts the database, returning ~${fmtBytes(db)} to disk. No messages or files are deleted.`,
          };
        }, 'Already fully compacted — nothing to reclaim.'));
    });
    ctl3.append(b3);
    r3.append(ctl3);
    section.append(r3);

    return section;
  }

  function daysInput(value, ariaLabel) {
    const wrap = el('label', 'dp-days');
    const input = document.createElement('input');
    input.type = 'number';
    input.min = '0';
    input.step = '1';
    input.value = String(value);
    // Row-specific label so a screen reader distinguishes the two spinbuttons.
    input.setAttribute('aria-label', ariaLabel || 'Age in days');
    wrap.append(input);
    wrap.append(el('span', 'dp-days-unit', 'days'));
    return { wrap, input };
  }

  // ── Per-channel breakdown ───────────────────────────────────────────────────
  function channelTable(panel, data) {
    const section = el('section', 'data-section');
    section.append(el('h3', null, 'By channel'));
    const rows = data.by_channel || [];
    if (!rows.length) { section.append(el('p', 'home-empty', 'No channels.')); return section; }

    const table = el('table', 'data-table');
    table.innerHTML = '<thead><tr>'
      + '<th>Channel</th><th class="num">Messages</th><th class="num">Msg size (est.)</th>'
      + '<th class="num">Files</th><th class="num">File size</th>'
      + '<th aria-label="Actions"></th>'
      + '</tr></thead>';
    const tbody = el('tbody');
    for (const c of rows) {
      const tr = el('tr', 'dt-row');
      const name = el('td');
      name.append(el('span', 'dt-chan', c.channel));
      if (c.archived) name.append(el('span', 'data-badge', 'archived'));
      tr.append(name);
      tr.append(numCell(c.message_count));
      tr.append(numCell(fmtBytes(c.est_message_bytes)));
      tr.append(numCell(c.attachment_count));
      tr.append(numCell(fmtBytes(c.attachment_bytes)));
      const act = el('td', 'dt-act');
      if (c.channel === INBOX) {
        // The agent inbox holds all DMs and is never deletable — say so rather
        // than leave a blank, unexplained undeletable row (Frodo).
        const note = el('span', 'dt-sys', 'system');
        note.title = 'The agent DM inbox — cannot be deleted';
        act.append(note);
      } else {
        const del = el('button', 'dp-btn danger sm', 'Delete');
        del.type = 'button';
        del.setAttribute('aria-label', `Delete channel ${c.channel}`);
        del.addEventListener('click', () => {
          withPending(del, () => previewThenConfirm(panel, { action: 'delete_channel', channel: c.channel },
            p => {
              const members = p.counts?.members || 0;
              // An ACTIVE (unarchived) channel deletion kicks connected agents
              // mid-task — escalate the warning vs an already-archived one (Frodo).
              const live = !c.archived
                ? `⚠ This channel is ACTIVE with ${plural(members, 'member')} connected — deleting it removes them mid-session. `
                : '';
              return {
                message: `Permanently delete the entire "${c.channel}" channel?`,
                description: live
                  + `Removes ${plural(p.counts?.messages || 0, 'message')}, `
                  + `${plural(p.counts?.attachments || 0, 'file')} `
                  + `(~${fmtBytes(p.would_free_bytes?.attachments || 0)}), and `
                  + `${plural(members, 'member')}. This cannot be undone.`,
              };
            }));
        });
        act.append(del);
      }
      tr.append(act);
      tbody.append(tr);
    }
    table.append(tbody);
    section.append(table);
    return section;
  }

  function numCell(v) { return el('td', 'num', String(v)); }

  // ── Entry point ─────────────────────────────────────────────────────────────
  async function renderPage(panel) {
    panel.replaceChildren();
    const head = el('div', 'page-head');
    head.append(el('h2', null, 'Data'));
    head.append(el('p', 'page-sub', "Manage storage — see what's using disk and prune old data."));
    panel.append(head);
    const body = el('div', 'data-body');
    body.append(el('p', 'home-empty', 'Loading…'));
    panel.append(body);

    let data;
    try {
      data = await api.get('/api/storage', false);
    } catch (e) {
      body.replaceChildren(el('p', 'home-empty', `Could not load storage: ${e.message || 'request failed'}`));
      return;
    }
    body.replaceChildren();
    body.append(overviewCards(data));
    body.append(pruneControls(panel));
    body.append(channelTable(panel, data));
  }

  Trio.data = { renderPage, fmtBytes };
})();
