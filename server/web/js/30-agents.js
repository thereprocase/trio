(() => {
  'use strict';
  const Trio = window.Trio;
  const $ = id => document.getElementById(id);
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const state = Trio.state;
  state.agentFilter = state.agentFilter || 'all';
  state.agentsSearch = state.agentsSearch || '';
  state.providers = state.providers || ['codex', 'claude'];
  // Empty until /api/agent-models answers. It used to be seeded with a
  // hardcoded catalogue, which meant a failed discovery silently offered
  // models that do not exist — the picker showed two stale Claude ids and two
  // stale Codex ones, and nothing told the operator the list was invented.
  state.agentModels = state.agentModels || {};
  state.discoveryLoading = false;
  let discoveryPromise = null;
  const pendingAgentActions = new Set();
  const compactionPolls = new Map();
  const PERMISSION_PROFILES = [
    ['observe', 'Observe (read-only)'],
    ['balanced', 'Balanced'],
    ['autonomous', 'Autonomous'],
  ];
  const typeInfo = {
    plan: { label: 'Plan', cls: 'type-plan' },
    command: { label: 'Command', cls: 'type-command' },
    tool: { label: 'Tool', cls: 'type-tool' },
    diff: { label: 'Diff', cls: 'type-diff' },
    file: { label: 'File', cls: 'type-file' },
    approval: { label: 'Approval', cls: 'type-approval' },
    warning: { label: 'Warning', cls: 'type-warning' },
    error: { label: 'Error', cls: 'type-error' },
  };
  function host() { let n = $('trio-agents'); if (!n) { n = document.createElement('aside'); n.id = 'trio-agents'; n.className = 'agent-drawer'; n.hidden = true; document.body.append(n); } return n; }
  function viewModel(agent = {}) {
    const lifecycle = agent.state === 'compacting' ? 'compacting' :
      (agent.live ? (agent.busy ? 'working' : 'idle') : (agent.state || 'offline'));
    return {
      id: agent.id,
      name: agent.name || agent.id,
      avatarUrl: agent.avatar_url || '',
      provider: agent.provider || 'unknown',
      model: agent.model || '',
      effort: agent.effort || '',
      kind: agent.kind || 'agent',
      lifecycle,
      statusText: agent.status_text || '',
      lastActive: agent.last_active || agent.last_active_at || agent.heartbeat,
      busy: !!agent.busy,
      live: !!agent.live,
      error: agent.error || '',
      placements: Array.isArray(agent.channels) ? agent.channels : (Array.isArray(agent.placements) ? agent.placements : []),
      wakePolicy: agent.wake_mode || agent.filter_mode || 'all',
      cwd: agent.cwd || '',
      permissions: agent.permission_profile || agent.permissions || '',
      compacting: lifecycle === 'compacting',
      archived: !!agent.archived || !!agent.archived_at,
      needsAttention: lifecycle === 'blocked' || lifecycle === 'errored' || lifecycle === 'error' || !!agent.error,
      contextPct: agent.context_pct == null ? null : Number(agent.context_pct),
    };
  }
  function actionCaps(vm) {
    const caps = [];
    if (vm.archived) return ['unarchive'];
    if (vm.lifecycle === 'compacting') return ['stop', 'archive'];
    if (vm.lifecycle === 'working' || vm.lifecycle === 'active') caps.push('stop');
    if (!vm.live && (vm.lifecycle === 'idle' || vm.lifecycle === 'sleeping' || vm.lifecycle === 'stopped' || vm.lifecycle === 'offline' || vm.lifecycle === 'stale')) caps.push('wake');
    if (vm.lifecycle === 'working' || vm.lifecycle === 'active') caps.push('interrupt');
    if (vm.lifecycle === 'working' || vm.lifecycle === 'active' || vm.lifecycle === 'idle') caps.push('hibernate');
    if (vm.lifecycle === 'idle') caps.push('compact');
    if (!['errored','blocked'].includes(vm.lifecycle)) caps.push('clear');
    caps.push('archive');
    return caps;
  }
  function actionLabel(action) {
    return ({
      stop: 'Stop',
      interrupt: 'Interrupt',
      hibernate: 'Hibernate',
      compact: 'Compact context',
      wake: 'Wake',
      clear: 'Clear context',
      archive: 'Archive agent',
      unarchive: 'Unarchive agent',
    })[action] || action;
  }
  function isDestructiveAction(action) { return action === 'clear' || action === 'archive'; }
  function confirmAction(vm, actionName) {
    if (!isDestructiveAction(actionName)) return true;
    if (actionName === 'archive') {
      let msg = 'Archive ' + vm.name + '? It stops the agent and hides it from the roster, but can be restored later.';
      if (vm.busy || vm.lifecycle === 'working' || vm.lifecycle === 'active')
        msg += '\n\nWarning: this agent is working — archiving will interrupt any work in progress.';
      else if (vm.lifecycle === 'compacting')
        msg += '\n\nWarning: this agent is compacting its context — archiving will interrupt it.';
      return window.confirm(msg);
    }
    return window.confirm('Clear context for ' + vm.name + '?');
  }
  function statusIcon(vm) {
    if (vm.needsAttention) return '!';
    if (vm.busy) return '●';
    if (vm.live) return '•';
    return '○';
  }
  function formatLastActive(value) {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString([], {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: 'numeric', minute: '2-digit',
    });
  }
  async function action(id, action, body = {}) {
    const key = id + ':' + action;
    if (pendingAgentActions.has(key)) return;
    pendingAgentActions.add(key);
    try {
      await Trio.api.post(`/api/agents/${encodeURIComponent(id)}/${action}`, body);
      await refresh();
      if (action === 'compact') watchCompaction(id);
    }
    catch (e) { Trio.ui.toast(e.message || 'Agent action failed'); }
    finally { pendingAgentActions.delete(key); }
  }
  function watchCompaction(id) {
    clearTimeout(compactionPolls.get(id));
    const poll = async () => {
      await refresh();
      const agent = (state.agents || []).find(candidate => candidate.id === id);
      if (agent?.state === 'compacting') compactionPolls.set(id, setTimeout(poll, 1500));
      else compactionPolls.delete(id);
    };
    const agent = (state.agents || []).find(candidate => candidate.id === id);
    if (agent?.state === 'compacting') compactionPolls.set(id, setTimeout(poll, 1500));
  }
  function agentCard(vm) {
    const article = document.createElement('article'); article.className = 'agent-card' + (vm.needsAttention ? ' needs-attention' : '') + (vm.archived ? ' is-archived' : '');
    article.innerHTML = `<b>${esc(vm.name)} ${esc(statusIcon(vm))}</b><small>${esc(vm.provider)}${vm.model ? ' · ' + esc(vm.model) : ''} · ${esc(vm.archived ? 'archived' : vm.lifecycle)}</small><p>${esc((vm.placements || []).join(', ') || 'No public rooms')}</p>`;
    const row = document.createElement('div'); row.className = 'agent-actions';
    const detail = document.createElement('button'); detail.type = 'button'; detail.textContent = 'Details'; detail.addEventListener('click', () => showDetail(vm)); row.append(detail);
    for (const a of actionCaps(vm).slice(0, 4)) {
      const b = document.createElement('button'); b.type = 'button'; b.textContent = a; b.dataset.action = a; b.dataset.id = vm.id;
      if (isDestructiveAction(a)) b.className = 'danger';
      b.addEventListener('click', () => confirmAction(vm, a) && action(vm.id, a));
      row.append(b);
    }
    if (!vm.archived) {
      const msg = document.createElement('button'); msg.type = 'button'; msg.textContent = 'Message'; msg.addEventListener('click', () => Trio.workspace?.openDmByKey?.(vm.id)); row.append(msg);
    }
    article.append(row);
    return article;
  }
  function matches(vm) {
    const f = state.agentFilter;
    if (f === 'archived') return vm.archived;
    if (f === 'active') return vm.live;
    if (f === 'working') return vm.busy;
    if (f === 'resting') return !vm.busy && !vm.needsAttention;
    if (f === 'needs-attention') return vm.needsAttention;
    return true;
  }
  function directoryCard(vm) {
    const article = document.createElement('article'); article.className = 'agent-card agent-card-openable' + (vm.needsAttention ? ' needs-attention' : '') + (vm.archived ? ' is-archived' : '') + (selection.has(vm.id) ? ' is-selected' : '');
    article.tabIndex = 0;
    article.setAttribute('aria-label', 'Manage agent ' + vm.name);
    const openDetails = event => {
      if (event?.target?.closest?.('button')) return;
      showDetail(vm);
    };
    article.addEventListener('click', openDetails);
    article.addEventListener('keydown', event => {
      if ((event.key === 'Enter' || event.key === ' ') && !event.target?.closest?.('button')) {
        event.preventDefault(); showDetail(vm);
      }
    });
    const initials = (vm.name || '?').split(/\s+/).map(part => part[0]).join('').slice(0, 2).toUpperCase();
    const tone = vm.archived ? 'var(--ink-3)' : vm.needsAttention ? 'var(--danger)' : vm.busy ? 'var(--warn)' : vm.live ? 'var(--ok)' : 'var(--accent)';
    article.style.setProperty('--card-accent', tone);
    // Per-agent backdrop tone — same gradient the facepile / details drawer use,
    // so the roster avatar sits on the agent's own colour, not the theme accent.
    const avTone = Trio.avatarTone(vm.name) || 'eucalyptus';
    const avatar = vm.avatarUrl ? `<span class="directory-avatar avatar-svg tone-${avTone}"><img src="${esc(vm.avatarUrl)}" alt="" class="avatar-svg-image"></span>` : `<span class="directory-avatar tone-${avTone}">${esc(initials)}</span>`;
    const chipCls = vm.archived ? 'archived' : vm.needsAttention ? 'offline' : vm.compacting || vm.busy ? 'thinking' : vm.live ? 'online' : 'idle';
    const chipLabel = vm.archived ? 'Archived' : vm.needsAttention ? 'Needs attention' : vm.compacting ? 'Compacting context…' : vm.busy ? 'Working' : vm.live ? 'Active' : 'Resting';
    const contextTag = vm.contextPct == null ? '' :
      `<span class="context-badge ${Trio.workspace.usageTone(vm.contextPct)}" title="${esc(String(Math.round(vm.contextPct)))}% of context window used">${esc(String(Math.round(vm.contextPct)))}% full</span>`;
    article.innerHTML = `<div class="ac-top">${avatar}<span><div class="ac-name">${esc(vm.name)}</div><div class="ac-role">${esc(vm.provider)}${vm.model ? ' · ' + esc(vm.model) : ''}</div></span></div><div class="ac-bio">${esc(vm.statusText || (vm.archived ? 'Archived — restore to rejoin.' : vm.live ? 'Connected and ready.' : 'Not currently connected.'))}</div><div class="ac-foot"><span class="status-chip ${chipCls}"><span class="st-dot"></span>${esc(chipLabel)}</span>${contextTag}${(vm.placements || []).slice(0, 2).map(p => `<span class="tag">#${esc(p)}</span>`).join('')}</div>`;
    // Selection checkbox: managing many agents at once starts here, so it is
    // always visible rather than behind a "select mode" toggle. It sits inside
    // the card but swallows its own clicks so ticking never opens Details.
    const pick = document.createElement('label'); pick.className = 'ac-pick';
    pick.title = 'Select for bulk actions';
    const box = document.createElement('input'); box.type = 'checkbox'; box.checked = selection.has(vm.id);
    box.setAttribute('aria-label', 'Select ' + vm.name);
    box.addEventListener('click', event => event.stopPropagation());
    box.addEventListener('change', () => { toggleSelected(vm.id, box.checked); });
    pick.addEventListener('click', event => event.stopPropagation());
    pick.append(box); article.append(pick);
    const actions = document.createElement('div'); actions.className = 'agent-actions';
    if (!vm.archived) {
      const message = document.createElement('button'); message.type = 'button'; message.textContent = 'Message'; message.addEventListener('click', () => Trio.workspace?.openDmByKey?.(vm.id)); actions.append(message);
    }
    article.append(actions); return article;
  }
  function renderPage(panel) {
    panel.replaceChildren();
    const hero = document.createElement('div'); hero.className = 'view-hero'; hero.innerHTML = '<h2>Agent roster</h2><p>Everyone working in this workspace</p>';
    const toolbar = document.createElement('div'); toolbar.className = 'roster-toolbar';
    const segment = document.createElement('div'); segment.className = 'seg';
    [['all','All'],['active','Active'],['working','Working'],['resting','Resting'],['needs-attention','Needs attention'],['archived','Archived']].forEach(([filter,label]) => { const b = document.createElement('button'); b.type = 'button'; b.textContent = label; b.className = state.agentFilter === filter ? 'on' : ''; b.addEventListener('click', () => { const crossing = (state.agentFilter === 'archived') !== (filter === 'archived'); state.agentFilter = filter; if (crossing) refresh(); else renderPage(panel); }); segment.append(b); });
    const search = document.createElement('input'); search.className = 'agent-page-search'; search.placeholder = 'Search agents…'; search.value = state.agentsSearch || ''; search.setAttribute('aria-label', 'Search agents'); search.addEventListener('input', () => { state.agentsSearch = search.value; renderPage(panel); });
    const createButton = document.createElement('button'); createButton.type = 'button'; createButton.className = 'btn primary'; createButton.textContent = 'New agent'; createButton.addEventListener('click', create);
    toolbar.append(segment, search, createButton);
    const grid = document.createElement('div'); grid.className = 'roster-grid';
    let list = (Trio.store.get('agents.list') || state.agents || []).map(viewModel).filter(matches);
    const query = (state.agentsSearch || '').trim().toLowerCase(); if (query) list = list.filter(vm => `${vm.name} ${vm.provider} ${vm.model}`.toLowerCase().includes(query));
    if (!list.length) {
      const empty = document.createElement('div'); empty.className = 'empty';
      // "Nothing matched your filter" and "this server cannot answer" are
      // different facts and must not share a message — one is the operator's
      // to fix, the other is not theirs at all.
      if (state.agentsUnavailable) {
        empty.innerHTML = '<div class="e-ic">✦</div><h3>Managed agents are off on this server</h3><p>This hub runs without the agent supervisor, so there is no roster to show. Nothing is wrong with your workspace.</p>';
      } else if (state.agentsError) {
        empty.innerHTML = `<div class="e-ic">✦</div><h3>Could not load the roster</h3><p>${esc(state.agentsError.message || 'The server did not answer.')}</p>`;
      } else {
        empty.innerHTML = '<div class="e-ic">✦</div><h3>No agents match</h3><p>Try another filter or invite someone new to the workspace.</p>';
      }
      grid.append(empty);
    }
    // "Select all" acts on what's currently VISIBLE (filter + search), which is
    // how the operator narrows a bulk target: filter to Resting, select all,
    // archive. Selecting the hidden remainder would be a nasty surprise.
    if (list.length) {
      const visible = list.map(vm => vm.id);
      const allOn = visible.every(id => selection.has(id));
      const selectAll = document.createElement('button'); selectAll.type = 'button'; selectAll.className = 'btn';
      selectAll.textContent = allOn ? 'Deselect all' : `Select all (${visible.length})`;
      selectAll.addEventListener('click', () => {
        visible.forEach(id => { if (allOn) selection.delete(id); else selection.add(id); });
        renderPage(panel);
      });
      toolbar.append(selectAll);
    }
    list.forEach(vm => grid.append(directoryCard(vm)));
    panel.append(hero, toolbar, grid);
    renderBulkBar();
  }
  // ── Bulk management ──────────────────────────────────────────────────────
  // One selection set shared by the roster page; every bulk operation posts to
  // /api/agents/bulk, which applies the action per agent and reports each
  // outcome, so a batch is never all-or-nothing.
  const selection = state.agentSelection instanceof Set ? state.agentSelection : new Set();
  state.agentSelection = selection;
  function rosterPanel() { const p = document.getElementById('trio-roster-view'); return p && !p.hidden ? p : null; }
  function repaintRoster() { const p = rosterPanel(); if (p) renderPage(p); }
  function toggleSelected(id, on) {
    if (on) selection.add(id); else selection.delete(id);
    repaintRoster();
  }
  function clearSelection() { selection.clear(); repaintRoster(); }
  // Same source renderPage draws from, and Array-guarded: state.agents is set
  // by several call sites and has been seen holding a non-array payload.
  function agentList() {
    const list = Trio.store.get('agents.list') || state.agents;
    return Array.isArray(list) ? list : [];
  }
  function selectedVms() {
    const byId = new Map(agentList().map(a => [a.id, viewModel(a)]));
    return [...selection].map(id => byId.get(id)).filter(Boolean);
  }
  function nameFor(id) {
    return agentList().find(a => a.id === id)?.name || id;
  }
  // Report a bulk result honestly: a toast when everything worked, a modal
  // listing agent + reason when some agents failed. Silent partial failure is
  // the one outcome a bulk tool must never produce.
  function reportBulk(res, label) {
    const total = res?.count ?? 0;
    const failed = (res?.results || []).filter(r => !r.ok);
    if (!failed.length) { Trio.ui.toast(`${label}: ${total} agent${total === 1 ? '' : 's'}`); return; }
    const done = total - failed.length;
    const rows = failed.map(r => `<div class="detail-row"><b>${esc(nameFor(r.agent_id))}</b><span>${esc(r.error || 'failed')}</span></div>`).join('');
    Trio.ui.modal(`${label}: ${done} of ${total} succeeded`,
      `<p>${esc(String(failed.length))} agent${failed.length === 1 ? '' : 's'} could not be updated:</p><div class="manage-detail">${rows}</div>`,
      undefined, { submit: false, cancelLabel: 'Close' });
  }
  const bulkPending = new Set();
  async function bulkAction(action, params = {}, { label, keepSelection = false } = {}) {
    const ids = [...selection];
    if (!ids.length) return null;
    if (bulkPending.has(action)) return null;
    bulkPending.add(action);
    try {
      const res = await Trio.api.post('/api/agents/bulk', { action, agent_ids: ids, params });
      if (!keepSelection) selection.clear();
      await refresh();
      reportBulk(res, label || actionLabel(action));
      return res;
    } catch (e) {
      Trio.ui.toast(e.message || 'Bulk action failed');
      return null;
    } finally { bulkPending.delete(action); }
  }
  function confirmBulk(action, count) {
    if (!isDestructiveAction(action)) return true;
    const noun = `${count} agent${count === 1 ? '' : 's'}`;
    if (action === 'archive') {
      const busy = selectedVms().filter(vm => vm.busy || vm.lifecycle === 'working' || vm.compacting).length;
      let msg = `Archive ${noun}? They stop and leave the roster, but can be restored later.`;
      if (busy) msg += `\n\nWarning: ${busy} of them ${busy === 1 ? 'is' : 'are'} mid-work — archiving interrupts it.`;
      return window.confirm(msg);
    }
    return window.confirm(`Clear context for ${noun}? Each starts a fresh session.`);
  }
  function renderBulkBar() {
    // Drop ids that are no longer listed (archived away, culled, or filtered
    // into the other archive view) so the count can't outlive its agents.
    const listed = new Set(agentList().map(a => a.id));
    [...selection].forEach(id => { if (!listed.has(id)) selection.delete(id); });
    const panel = rosterPanel(); if (!panel) return;
    if (!selection.size) return;
    const bar = document.createElement('div'); bar.className = 'bulk-bar'; bar.setAttribute('role', 'region'); bar.setAttribute('aria-label', 'Bulk agent actions');
    const count = document.createElement('span'); count.className = 'bulk-count';
    count.textContent = `${selection.size} selected`;
    const actions = document.createElement('div'); actions.className = 'bulk-actions';
    const add = (label, handler, variant) => {
      const b = document.createElement('button'); b.type = 'button';
      b.className = 'mbtn' + (variant ? ' ' + variant : ''); b.textContent = label;
      b.addEventListener('click', handler); actions.append(b); return b;
    };
    if (state.agentFilter === 'archived') {
      add('Unarchive', () => bulkAction('unarchive', {}, { label: 'Unarchived' }), 'primary');
    } else {
      add('Wake', () => bulkAction('wake', {}, { label: 'Woke' }));
      add('Hibernate', () => bulkAction('hibernate', {}, { label: 'Hibernated' }));
      add('Stop', () => bulkAction('stop', {}, { label: 'Stopped' }));
      add('Compact…', showBulkCompact);
      add('Clear context', () => confirmBulk('clear', selection.size) && bulkAction('clear', {}, { label: 'Cleared' }), 'danger');
      add('Attributes…', showBulkAttributes);
      add('Channels…', showBulkChannels);
      add('Archive', () => confirmBulk('archive', selection.size) && bulkAction('archive', {}, { label: 'Archived' }), 'danger');
    }
    const clear = document.createElement('button'); clear.type = 'button'; clear.className = 'bulk-clear'; clear.textContent = 'Clear selection';
    clear.addEventListener('click', clearSelection);
    bar.append(count, actions, clear);
    panel.append(bar);
  }
  function showBulkCompact() {
    const n = selection.size;
    const html = `<p>Summarize the context of ${esc(String(n))} agent${n === 1 ? '' : 's'}, keeping the work that matters. Sleeping agents are woken first.</p>`
      + '<label class="field">What should be preserved? <textarea name="compaction-message" maxlength="2000" placeholder="Optional: key decisions, constraints, or next steps to retain"></textarea></label>';
    Trio.ui.modal(`Compact context: ${n} agent${n === 1 ? '' : 's'}`, html, node => {
      const message = (node.querySelector('[name="compaction-message"]')?.value || '').trim();
      bulkAction('compact', { message }, { label: 'Compacting' });
    });
  }
  const UNCHANGED = '__unchanged__';
  // Form values -> the bulk calls they imply. Pure so the mapping (which is
  // where "unchanged" vs "cleared" is decided) can be tested without a DOM:
  // an untouched field sends nothing, an empty effort means "model default",
  // and an empty cwd only clears when the operator opted in via the checkbox.
  function bulkAttributeJobs(fields = {}) {
    const jobs = [];
    const { model, effort, wake, permission_profile: perms } = fields;
    if (model && model !== UNCHANGED) jobs.push(['model', { model }, 'Model changed']);
    if (effort != null && effort !== UNCHANGED) jobs.push(['effort', { effort }, 'Effort changed']);
    if (wake && wake !== UNCHANGED) jobs.push(['wake-mode', { mode: wake }, 'Wake policy changed']);
    if (perms && perms !== UNCHANGED) jobs.push(['permissions', { permission_profile: perms }, 'Permissions changed']);
    if (fields.cwd_apply) jobs.push(['cwd', { cwd: (fields.cwd || '').trim() }, 'Working directory changed']);
    return jobs;
  }
  // Attribute editor for a mixed selection. Every field defaults to "Leave
  // unchanged" and only changed fields are sent — each as its own bulk call,
  // because the server validates model/effort per agent and reports per agent.
  function showBulkAttributes() {
    const vms = selectedVms();
    const n = vms.length;
    const providers = [...new Set(vms.map(vm => vm.provider))];
    // Model ids are provider-specific, so offering a model list across a
    // mixed-provider selection would produce guaranteed per-agent failures.
    const modelField = providers.length === 1
      ? `<label class="field">Model <select name="model"><option value="${UNCHANGED}" selected>Leave unchanged</option>${modelOptions(state.agentModels[providers[0]])}</select><span class="cfg-hint">Applies when each agent next starts a process — Clear for a fresh session, or Wake.</span></label>`
      : `<div class="field"><b>Model</b><p class="cfg-hint">Selection spans ${esc(providers.join(' + '))} — select one provider's agents to change models.</p></div>`;
    // Effort sets differ per model; offer the union of what the selection's
    // models advertise and let the server reject any agent that can't take it.
    const efforts = [...new Set(vms.flatMap(vm => effortsForModel(vm.provider, vm.model)))];
    const effortField = `<label class="field">Reasoning effort <select name="effort"><option value="${UNCHANGED}" selected>Leave unchanged</option><option value="">Model default</option>${efforts.map(e => `<option value="${esc(e)}">${esc(capWord(e))}</option>`).join('')}</select></label>`;
    const wakeField = `<label class="field">Wake policy <select name="wake"><option value="${UNCHANGED}" selected>Leave unchanged</option>${WAKE_STEPS.map(s => `<option value="${esc(s)}">${esc(capWord(s))}</option>`).join('')}</select></label>`;
    const permField = `<label class="field">Permission profile <select name="permission_profile"><option value="${UNCHANGED}" selected>Leave unchanged</option>${PERMISSION_PROFILES.map(([value, label]) => `<option value="${esc(value)}">${esc(label)}</option>`).join('')}</select></label>`;
    // cwd needs an explicit opt-in: an empty text box means "clear the working
    // directory", which is a real edit and must not be confused with "leave it".
    const cwdField = '<label class="field"><input type="checkbox" name="cwd_apply"> Set working directory</label>'
      + '<label class="field"><input name="cwd" placeholder="/path/to/project" disabled><span class="cfg-hint">Leave the path blank to clear it. Applies on each agent\'s next process start.</span></label>';
    const html = `<p>Editing ${esc(String(n))} agent${n === 1 ? '' : 's'}. Only the fields you change are applied.</p>`
      + modelField + effortField + wakeField + permField + cwdField;
    Trio.ui.modal(`Attributes: ${n} agent${n === 1 ? '' : 's'}`, html, async node => {
      const f = new FormData(node.querySelector('form'));
      const jobs = bulkAttributeJobs({
        model: f.get('model'), effort: f.get('effort'), wake: f.get('wake'),
        permission_profile: f.get('permission_profile'),
        cwd_apply: !!f.get('cwd_apply'), cwd: f.get('cwd'),
      });
      if (!jobs.length) { Trio.ui.toast('Nothing to change'); return; }
      // Keep the selection until the last job so every field lands on the same
      // set of agents, then drop it once.
      for (let i = 0; i < jobs.length; i++) {
        const [action, params, label] = jobs[i];
        await bulkAction(action, params, { label, keepSelection: i < jobs.length - 1 });
      }
    });
    const panel = document.getElementById('trio-control-modal');
    const applyBox = panel?.querySelector('[name="cwd_apply"]');
    const cwdInput = panel?.querySelector('[name="cwd"]');
    applyBox?.addEventListener('change', () => { if (cwdInput) cwdInput.disabled = !applyBox.checked; });
  }
  function showBulkChannels() {
    const n = selection.size;
    const codes = chanCodesFor();
    const html = `<p>Add or remove ${esc(String(n))} agent${n === 1 ? '' : 's'} across the channels you pick.</p>`
      + '<div class="field"><label><input type="radio" name="mode" value="add" checked> Add to selected channels</label>'
      + '<label><input type="radio" name="mode" value="remove"> Remove from selected channels</label></div>'
      + channelListMarkup(codes, new Set());
    let channelApi = null;
    Trio.ui.modal(`Channels: ${n} agent${n === 1 ? '' : 's'}`, html, node => {
      const present = (node.querySelector('[name="mode"]:checked')?.value || 'add') === 'add';
      const channels = channelApi ? [...channelApi.getSelected()] : [];
      if (!channels.length) { Trio.ui.toast('Pick at least one channel'); return; }
      bulkAction('placement', { channels, present },
        { label: present ? 'Added to channels' : 'Removed from channels' });
    });
    const panel = document.getElementById('trio-control-modal');
    if (panel) channelApi = wireChannelList(panel);
  }
  function render(agents = Trio.store.get('agents.list')) {
    const n = host();
    let list = (agents || []).map(a => viewModel(a)).filter(matches);
    if (state.agentsSearch) {
      const q = state.agentsSearch.toLowerCase();
      list = list.filter(vm => (vm.name + ' ' + vm.model + ' ' + vm.provider).toLowerCase().includes(q));
    }
    n.innerHTML = `<button class="modal-close" aria-label="Close">×</button><h2>Agent roster</h2><div class="agent-filters">${['all','active','working','resting','needs-attention','archived'].map(f => `<button data-filter="${f}" class="${state.agentFilter === f ? 'active' : ''}">${f.replace(/-/g,' ')}</button>`).join('')}<button type="button" class="agent-new">New agent</button></div><input class="agent-search" placeholder="Search agents…" value="${esc(state.agentsSearch)}"><div class="agent-list">${list.length ? '' : '<p>No agents match.</p>'}</div>`;
    n.querySelector('.modal-close').onclick = () => n.hidden = true;
    n.querySelector('.agent-new').onclick = () => create();
    const listNode = n.querySelector('.agent-list');
    list.forEach(vm => listNode.append(agentCard(vm)));
    n.querySelectorAll('[data-filter]').forEach(b => b.onclick = () => { const crossing = (state.agentFilter === 'archived') !== (b.dataset.filter === 'archived'); state.agentFilter = b.dataset.filter; if (crossing) refresh(); else render(agents); });
    const input = n.querySelector('.agent-search');
    input.oninput = () => { state.agentsSearch = input.value; render(agents); };
  }
  function showDetail(vm) {
    const rows = [
      ['Name', vm.name], ['Provider', vm.provider], ['Model', vm.model], ['Lifecycle', vm.lifecycle],
      ['Reasoning effort', vm.effort || 'default'], ['Wake policy', vm.wakePolicy], ['Cwd', vm.cwd], ['Permissions', vm.permissions], ['Placements', (vm.placements || []).join(', ') || 'none'],
      ['Last active', formatLastActive(vm.lastActive)], ['Status', vm.statusText || ''], ['Error', vm.error || ''],
    ];
    const caps = actionCaps(vm);
    const detail = '<div class="manage-detail">'
      + rows.map(([k, v]) => `<div class="detail-row"><b>${esc(k)}</b><span>${esc(v)}</span></div>`).join('')
      + '</div>';
    // Grouped, labelled action sections (Lifecycle / Context / Configure /
    // Danger zone) with consistent .mbtn styling — replaces two flat rows of
    // tiny unstyled buttons so a destructive action never sits flush against a
    // routine one and the primary action reads as primary.
    const btn = (name, variant, opts = {}) => {
      const attrs = [`data-agent-action="${esc(name)}"`, 'type="button"', `class="mbtn ${variant || ''}"`];
      if (opts.disabled) attrs.push('disabled');
      if (opts.title) attrs.push(`title="${esc(opts.title)}"`);
      return `<button ${attrs.join(' ')}>${esc(actionLabel(name))}</button>`;
    };
    const section = (label, inner, endAlign) => inner
      ? `<div class="manage-section"><span class="manage-label">${esc(label)}</span><div class="manage-actions${endAlign ? ' end' : ''}">${inner}</div></div>`
      : '';
    let sections;
    if (vm.archived) {
      sections = section('Lifecycle', btn('unarchive', ''));
    } else {
      const life = ['wake', 'stop', 'interrupt', 'hibernate'].filter(a => caps.includes(a)).map(a => btn(a, '')).join('');
      // Compact is always shown (per request) but only enabled when idle — the
      // one state the backend accepts it. Otherwise it's a visible disabled
      // button that explains why, rather than hidden or one that errors on click.
      const compactBtn = btn('compact', 'primary', caps.includes('compact') ? {} : {
        disabled: true,
        title: vm.live ? 'Compacting is available when the agent is idle' : 'Wake the agent to compact its context',
      });
      const clearBtn = caps.includes('clear') ? btn('clear', 'danger') : '';
      // Configure is an inline editor: a filterable channel-placement list plus
      // wake-policy and reasoning-effort sliders, committed together by one Save
      // button that only enables once something actually changes.
      const chanCodes = (state.channels || []).filter(c => !c.archived).map(c => c.code);
      const placementsBlock = '<div class="cfg-block"><div class="cfg-title">Channel placements</div>'
        + channelListMarkup(chanCodes, new Set(vm.placements || [])) + '</div>';
      const wakeBlock = '<div class="cfg-block">'
        + discreteSlider('wake', 'Wake policy', WAKE_STEPS, WAKE_STEPS.includes(vm.wakePolicy) ? vm.wakePolicy : 'all')
        + '<span class="cfg-hint">How much wakes this agent — <b>all</b>: every message · <b>about</b>: @/#-mentions of it + bangs · <b>at</b>: only @-mentions + bangs.</span></div>';
      const effortBlock = '<div class="cfg-block" id="cfg-effort">'
        + effortSlider(effortsForModel(vm.provider, vm.model), vm.effort, { defaultLabel: effortDefaultLabel(vm.provider, vm.model), modelDefault: modelDefaultEffort(vm.provider, vm.model) }) + '</div>';
      const cfgEditor = '<div class="cfg-editor">' + placementsBlock + wakeBlock + effortBlock
        + '<div class="cfg-foot"><button type="button" class="mbtn primary" data-cfg-save disabled>Save changes</button></div></div>';
      sections = section('Lifecycle', life)
        + section('Context', compactBtn + clearBtn)
        + `<div class="manage-section"><span class="manage-label">Configure</span>${cfgEditor}</div>`
        + (caps.includes('archive') ? section('Danger zone', btn('archive', 'danger'), true) : '');
    }
    const html = detail + sections;
    Trio.ui.modal('Manage agent: ' + vm.name, html, undefined, { submit: false, cancelLabel: 'Close' });
    setTimeout(() => {
      const panel = document.getElementById('trio-control-modal');
      if (!panel) return;
      panel.querySelectorAll('[data-agent-action]').forEach(button => button.addEventListener('click', () => {
        const actionName = button.dataset.agentAction;
        if (actionName === 'compact') {
          panel.close('cancel');
          setTimeout(() => showCompact(vm), 0);
        } else if (confirmAction(vm, actionName)) {
          panel.close('cancel');
          let toastMsg = actionLabel(actionName) + ' requested for ' + vm.name;
          if (actionName === 'unarchive') toastMsg += ' — Wake it to resume';
          Trio.ui.toast(toastMsg);
          action(vm.id, actionName);
        }
      }));
      wireConfigureEditor(panel, vm, chanCodesFor(vm));
    }, 0);
  }
  function showCompact(vm) {
    const html = `<p>Summarize this agent's context to free room while retaining the important work.</p><label class="field">What should be preserved? <textarea name="compaction-message" maxlength="2000" placeholder="Optional: key decisions, constraints, or next steps to retain"></textarea></label>`;
    Trio.ui.modal('Compact context: ' + vm.name, html, node => {
      const message = (node.querySelector('[name="compaction-message"]')?.value || '').trim();
      action(vm.id, 'compact', { message });
    });
  }
  const WAKE_STEPS = ['all', 'about', 'at'];
  function chanCodesFor() { return (state.channels || []).filter(c => !c.archived).map(c => c.code); }
  // Efforts are per-MODEL (a Sonnet agent and a Haiku agent don't support the
  // same set) — look the agent's own model up in the already-discovered
  // provider model list rather than offering a fixed global list.
  function effortModelEntry(provider, modelId) {
    return normalizeModels(state.agentModels[provider]).find(m => m.id === modelId);
  }
  // [] when the model is not in the discovered catalogue. This used to fall
  // back to ['low','medium','high'], which is wrong for nearly every model:
  // Codex offers xhigh/max/ultra and Haiku has no max. Presenting a ladder the
  // model does not have is worse than presenting none, because the operator
  // picks a value that is then silently coerced.
  function effortsForModel(provider, modelId) {
    const model = effortModelEntry(provider, modelId);
    return Array.isArray(model?.efforts) && model.efforts.length ? model.efforts : [];
  }
  // LOTC/Frodo: with no "use the model's own default" option, the browser
  // auto-selects the FIRST <option> whenever `selected` matches nothing —
  // which silently downgraded every unedited agent to the lowest effort
  // (both at creation and when merely opening-then-saving the edit dialog,
  // since an agent at its default has vm.effort === ''). A real, always-
  // present "Model default" option fixes both: it's what actually gets sent
  // when the user doesn't touch the control, matching what the backend
  // already does with an empty effort string.
  // `current` (if given and not already in `efforts`) is kept as an option
  // rather than dropped — an agent already running at "max" must not lose
  // that from the list just because live discovery came back stale/thin.
  function effortOptions(efforts, selected, { defaultLabel = 'Model default' } = {}) {
    const all = current => current && !efforts.includes(current) ? [...efforts, current] : efforts;
    const list = all(selected);
    const opts = list.map(e => `<option value="${esc(e)}"${e === selected ? ' selected' : ''}>${esc(e)}</option>`).join('');
    return `<option value=""${selected ? '' : ' selected'}>${esc(defaultLabel)}</option>` + opts;
  }
  // "Default value should be the last value we picked for that agent." We
  // persist the last effort chosen per provider+model in localStorage so the
  // Create-agent slider re-opens where you left it. (Editing an existing agent
  // defaults to that agent's own current effort — see the manage-dialog
  // Configure editor.)
  const EFFORT_KEY = 'trio.effort.last';
  function lastEffort(provider, model) {
    try { return JSON.parse(localStorage.getItem(EFFORT_KEY) || '{}')[`${provider}:${model}`] ?? null; }
    catch { return null; }
  }
  function rememberEffort(provider, model, value) {
    try {
      const map = JSON.parse(localStorage.getItem(EFFORT_KEY) || '{}');
      map[`${provider}:${model}`] = value || '';
      localStorage.setItem(EFFORT_KEY, JSON.stringify(map));
    } catch { /* private mode / quota — non-fatal */ }
  }
  const capWord = s => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);
  // The scale tick is a SHORT label — the leftmost step is the special
  // "Default" (not a magnitude), the rest are the capitalized effort names.
  const tickLabel = step => step === '' || step == null ? 'Default' : capWord(step);
  // The live readout / aria value shows the full model-default label at step 0,
  // otherwise the capitalized effort. (Rendered case is authored here, not via
  // CSS text-transform, so the multi-word default label isn't title-cased.)
  const valueDisplay = (step, defaultLabel) => step === '' || step == null ? defaultLabel : capWord(step);
  // A discrete slider over ["" (model default), ...efforts]. The visible knob
  // drives a hidden <input name="effort"> so the form reads exactly like the
  // old <select>. `selected` (an agent's current / last-picked effort) that
  // isn't in the discovered list is appended rather than dropped.
  function effortSlider(efforts, selected, { defaultLabel = 'Not set', modelDefault = '' } = {}) {
    // Nothing to choose from yet. Say which of the two reasons it is rather
    // than rendering a ladder the model may not have — the operator can act on
    // "pick a model", and cannot act on an invented low/medium/high.
    if (!efforts.length) {
      const why = selected
        ? `Currently ${esc(selected)}. The available levels for this model could not be loaded.`
        : 'Choose a model first — each one offers different levels.';
      return `<div class="field effort-field effort-unavailable">`
        + `<label>Reasoning effort</label><p class="hint">${why}</p>`
        + `<input type="hidden" name="effort" value="${esc(selected || '')}"></div>`;
    }
    const sel = selected || '';
    const steps = ['', ...efforts];
    if (sel && !steps.includes(sel)) steps.push(sel);
    const idx = Math.max(0, steps.indexOf(sel));
    const n = steps.length;
    // Ticks are absolutely positioned at each thumb stop (i/(n-1)) so the label
    // sits under the knob position, not at flex-column centers that drift off.
    const scale = steps.map((s, i) => {
      const pct = n > 1 ? (i / (n - 1)) * 100 : 0;
      return `<span class="${i === idx ? 'on' : ''}" style="left:${pct}%">${esc(tickLabel(s))}</span>`;
    }).join('');
    const display = esc(valueDisplay(sel, defaultLabel));
    return `<div class="field effort-field" data-steps="${esc(JSON.stringify(steps))}" data-default-label="${esc(defaultLabel)}">`
      + `<label for="effort-range">Reasoning effort <span class="hint" id="effort-hint">${modelDefault ? 'this model runs ' + esc(modelDefault) + ' on its own' : ''}</span></label>`
      + `<div class="effort-slider"><div class="effort-track"><input type="range" id="effort-range" class="effort-range" min="0" max="${n - 1}" step="1" value="${idx}" aria-describedby="effort-value" aria-valuetext="${display}"><div class="effort-scale">${scale}</div></div><output id="effort-value" class="effort-value">${display}</output></div>`
      + `<input type="hidden" name="effort" value="${esc(sel)}">`
      + `</div>`;
  }
  function wireEffortSlider(root, onInput) {
    const field = root.querySelector('.effort-field'); if (!field) return null;
    const range = field.querySelector('.effort-range');
    const out = field.querySelector('.effort-value');
    const hidden = field.querySelector('input[name="effort"]');
    const ticks = [...field.querySelectorAll('.effort-scale span')];
    // data-steps is always our own esc(JSON.stringify(...)) output, but guard
    // anyway so a malformed attribute degrades to a default-only slider rather
    // than throwing and killing the control.
    let steps; try { steps = JSON.parse(field.dataset.steps || '[""]'); } catch { steps = ['']; }
    if (!Array.isArray(steps) || !steps.length) steps = [''];
    const defaultLabel = field.dataset.defaultLabel || 'Model default';
    const sync = () => {
      const i = Number(range.value);
      const value = steps[i] ?? '';
      hidden.value = value;
      const display = valueDisplay(value, defaultLabel);
      out.textContent = display;
      range.setAttribute('aria-valuetext', display); // SR reads "medium", not "3"
      ticks.forEach((t, ti) => t.classList.toggle('on', ti === i));
      onInput?.(value);
    };
    range.addEventListener('input', sync); sync();
    return { sync };
  }
  // The unset position is labelled "Not set", not "Model default": what the
  // model would do on its own is INFORMATION (shown in the hint), whereas
  // "Default" reads as a choice whose behaviour nobody can state. The unset
  // position still exists so the slider cannot silently auto-select the lowest
  // level for an agent the operator never touched — create() refuses to submit
  // while it is unset, so the choice ends up explicit either way.
  function effortDefaultLabel(provider, model) {
    return 'Not set';
  }
  function modelDefaultEffort(provider, model) {
    return effortModelEntry(provider, model)?.default_effort || '';
  }
  // Sauron: when an effort change takes hold differs by provider. Codex re-reads
  // effort fresh every turn; Claude fixes it in the process argv at spawn, so
  // only Clear (a fresh session) is guaranteed to pick it up — Wake may not.
  function effortHint(provider) {
    return provider === 'codex'
      ? 'applies to this agent’s next message — no restart needed'
      : 'applies once this agent is Cleared (a fresh session) — Wake resumes the existing session and may not pick it up';
  }
  // ── Shared channel selector ──────────────────────────────────────────────
  // Filterable, scrollable list of channel tiles: click a tile to toggle; the
  // selected tile is outlined (no checkbox). Selection lives in the DOM
  // (`.selected` class) and is read back through wireChannelList().getSelected().
  function channelListMarkup(codes, selected, { empty = 'No channels available.' } = {}) {
    if (!codes.length) return `<p class="home-empty">${esc(empty)}</p>`;
    const tiles = codes.map(c =>
      `<button type="button" class="chan-tile${selected.has(c) ? ' selected' : ''}" data-code="${esc(c)}" role="option" aria-selected="${selected.has(c) ? 'true' : 'false'}">#${esc(c)}</button>`
    ).join('');
    return '<div class="chan-select">'
      + '<input type="text" class="chan-filter" placeholder="Filter channels…" aria-label="Filter channels">'
      + `<div class="chan-list" role="listbox" aria-multiselectable="true">${tiles}</div>`
      + '<p class="chan-empty" hidden>No channels match.</p></div>';
  }
  function wireChannelList(root, onChange) {
    const box = root.querySelector('.chan-select');
    if (!box) return { getSelected: () => new Set() };
    const filter = box.querySelector('.chan-filter');
    const empty = box.querySelector('.chan-empty');
    const tiles = [...box.querySelectorAll('.chan-tile')];
    const selected = new Set(tiles.filter(t => t.classList.contains('selected')).map(t => t.dataset.code));
    filter?.addEventListener('input', () => {
      const q = filter.value.trim().toLowerCase();
      let shown = 0;
      tiles.forEach(t => { const hide = !!q && !t.dataset.code.toLowerCase().includes(q); t.hidden = hide; if (!hide) shown++; });
      // Distinguish "filtered to nothing" from a genuinely empty list, so the
      // area never reads as a broken filter (Frodo).
      if (empty) empty.hidden = shown > 0;
    });
    tiles.forEach(t => t.addEventListener('click', () => {
      const on = t.classList.toggle('selected');
      t.setAttribute('aria-selected', on ? 'true' : 'false');
      if (on) selected.add(t.dataset.code); else selected.delete(t.dataset.code);
      onChange?.(new Set(selected));
    }));
    return { getSelected: () => new Set(selected) };
  }
  // ── Generic discrete slider ──────────────────────────────────────────────
  // Reuses the effort-slider styling for any small ordered option set (e.g.
  // wake policy). Drives a hidden input named `name`; read via wireSlider().get().
  function discreteSlider(name, label, steps, selected, labelFor = capWord) {
    const idx = Math.max(0, steps.indexOf(selected));
    const n = steps.length;
    const uid = 'slider-' + name;
    const scale = steps.map((s, i) => {
      const pct = n > 1 ? (i / (n - 1)) * 100 : 0;
      return `<span class="${i === idx ? 'on' : ''}" style="left:${pct}%">${esc(labelFor(s))}</span>`;
    }).join('');
    const disp = esc(labelFor(selected));
    return `<div class="field slider-field" data-steps="${esc(JSON.stringify(steps))}">`
      + `<label for="${uid}">${esc(label)}</label>`
      + `<div class="effort-slider"><div class="effort-track"><input type="range" id="${uid}" class="effort-range" min="0" max="${n - 1}" step="1" value="${idx}" aria-valuetext="${disp}"><div class="effort-scale">${scale}</div></div><output class="effort-value">${disp}</output></div>`
      + `<input type="hidden" name="${esc(name)}" value="${esc(selected)}"></div>`;
  }
  function wireSlider(fieldEl, labelFor, onInput) {
    if (!fieldEl) return { get: () => '' };
    const range = fieldEl.querySelector('.effort-range');
    const out = fieldEl.querySelector('.effort-value');
    const hidden = fieldEl.querySelector('input[type="hidden"]');
    const ticks = [...fieldEl.querySelectorAll('.effort-scale span')];
    let steps; try { steps = JSON.parse(fieldEl.dataset.steps || '[]'); } catch { steps = []; }
    // `notify` gates the onInput callback: the initial render is layout-only so
    // it can't fire onInput before the caller's own bindings (e.g. a `const`
    // captured inside the callback) are initialized — that would be a TDZ crash.
    const render = notify => {
      const i = Number(range.value);
      const v = steps[i] ?? '';
      hidden.value = v;
      const d = labelFor(v);
      out.textContent = d;
      range.setAttribute('aria-valuetext', d);
      ticks.forEach((t, ti) => t.classList.toggle('on', ti === i));
      if (notify) onInput?.(v);
    };
    range.addEventListener('input', () => render(true));
    render(false);
    return { get: () => hidden.value };
  }
  // ── Manage-dialog Configure editor ───────────────────────────────────────
  // Wires the inline placements list + wake/effort sliders. Save enables only
  // when placements, wake, or effort differ from the agent's current state, and
  // commits all diffs at once. Effort steps are refined once live discovery
  // (per-model effort sets) resolves.
  function wireConfigureEditor(panel, vm, chanCodes) {
    const save = panel.querySelector('[data-cfg-save]');
    if (!save) return;
    // Baseline placements must be drawn from the SAME universe as the tiles
    // (non-archived channels only) — otherwise a placement in a since-archived
    // channel, which has no tile to match, makes the set permanently unequal,
    // leaving Save falsely enabled and then no-op on click.
    const initialPlacements = new Set((vm.placements || []).filter(c => chanCodes.includes(c)));
    const initialWake = WAKE_STEPS.includes(vm.wakePolicy) ? vm.wakePolicy : 'all';
    const initialEffort = vm.effort || '';
    const setsEqual = (a, b) => a.size === b.size && [...a].every(x => b.has(x));
    const effortValue = () => panel.querySelector('#cfg-effort input[name="effort"]')?.value ?? '';
    const chan = wireChannelList(panel, () => refreshDirty());
    const wake = wireSlider(panel.querySelector('.slider-field'), capWord, () => refreshDirty());
    function refreshDirty() {
      const dirty = !setsEqual(chan.getSelected(), initialPlacements)
        || wake.get() !== initialWake
        || effortValue() !== initialEffort;
      save.disabled = !dirty;
    }
    const effortHost = panel.querySelector('#cfg-effort');
    if (effortHost) {
      wireEffortSlider(effortHost, () => refreshDirty());
      const h = effortHost.querySelector('#effort-hint'); if (h) h.textContent = effortHint(vm.provider);
    }
    // Refine effort steps to the model's real set once discovery resolves — but
    // don't yank the control out from under a user who's mid-interaction, and
    // skip the rebuild entirely if the step set is unchanged. Preserve the live
    // selection via effortValue() (NOT `|| initialEffort`, which would silently
    // revert a deliberate "model default" pick back to the agent's old effort).
    loadDiscovery().then(() => {
      if (!effortHost || !effortHost.isConnected) return;
      if (effortHost.contains(document.activeElement)) return;
      const efforts = effortsForModel(vm.provider, vm.model);
      const field = effortHost.querySelector('.effort-field');
      let curSteps = []; try { curSteps = JSON.parse(field?.dataset.steps || '[]'); } catch { /* keep [] */ }
      const newSteps = ['', ...efforts];
      if (curSteps.length === newSteps.length && curSteps.every((s, i) => s === newSteps[i])) return;
      effortHost.innerHTML = effortSlider(efforts, effortValue(),
        { defaultLabel: effortDefaultLabel(vm.provider, vm.model) });
      wireEffortSlider(effortHost, () => refreshDirty());
      const h = effortHost.querySelector('#effort-hint'); if (h) h.textContent = effortHint(vm.provider);
      refreshDirty();
    }).catch(() => { /* discovery best-effort; fallback steps already shown */ });
    save.addEventListener('click', async () => {
      save.disabled = true;
      const selected = chan.getSelected();
      const wakeVal = wake.get();
      const effortVal = effortValue();
      const changedChannels = chanCodes.filter(code => selected.has(code) !== initialPlacements.has(code));
      // Post directly (not via action(), which swallows its own errors) so a
      // failed change is actually surfaced: keep the dialog open, re-enable
      // Save, and never claim success it didn't achieve.
      try {
        for (const channel of changedChannels) await Trio.api.post(`/api/agents/${encodeURIComponent(vm.id)}/placement`, { channel, present: selected.has(channel) });
        if (wakeVal !== initialWake) await Trio.api.post(`/api/agents/${encodeURIComponent(vm.id)}/wake-mode`, { mode: wakeVal });
        if (effortVal !== initialEffort) { rememberEffort(vm.provider, vm.model, effortVal); await Trio.api.post(`/api/agents/${encodeURIComponent(vm.id)}/effort`, { effort: effortVal }); }
        panel.close('cancel');
        Trio.ui.toast('Changes saved for ' + vm.name);
        await refresh();
      } catch (e) {
        save.disabled = false;
        Trio.ui.toast(e.message || 'Could not save changes');
      }
    });
  }
  function normalizeModels(models) {
    if (!Array.isArray(models)) return [];
    return models.map(model => {
      if (typeof model === 'string') return { id: model, name: model };
      if (!model || typeof model !== 'object') return null;
      const id = model.id || model.model;
      return id ? { ...model, id, name: model.name || model.displayName || id } : null;
    }).filter(Boolean);
  }
  function modelOptions(models) {
    return normalizeModels(models).map(model => `<option value="${esc(model.id)}">${esc(model.name)}</option>`).join('');
  }
  function permissionOptions(selected = 'balanced') {
    return `<select name="permission_profile">${PERMISSION_PROFILES.map(([value, label]) => `<option value="${value}"${value === selected ? ' selected' : ''}>${label}</option>`).join('')}</select>`;
  }
  async function loadDiscovery() {
    if (discoveryPromise) return discoveryPromise;
    discoveryPromise = (async () => {
      state.discoveryLoading = true;
      try {
        const health = await Trio.api.get('/api/health').catch(() => ({}));
        const providers = Array.isArray(health.providers) ? health.providers : Object.keys(health.runtimes || {});
        if (providers.length) state.providers = [...new Set(providers.map(p => String(p).toLowerCase()))];
        const discovered = await Promise.all(state.providers.map(async provider => {
          const data = await Trio.api.get(`/api/agent-models?provider=${encodeURIComponent(provider)}`).catch(() => null);
          return data && Array.isArray(data.models) ? [provider, normalizeModels(data.models)] : null;
        }));
        const nextModels = { ...state.agentModels };
        discovered.filter(Boolean).forEach(([provider, models]) => { nextModels[provider] = models; });
        state.agentModels = nextModels;
      } catch (e) { console.warn('discovery failed', e); }
      finally {
        state.discoveryLoading = false;
        discoveryPromise = null;
      }
      return state.agentModels;
    })();
    return discoveryPromise;
  }
  async function refresh() {
    try {
      const archived = state.agentFilter === 'archived';
      const data = await Trio.api.get('/api/agents' + (archived ? '?archived=1' : ''));
      state.agents = data.agents || [];
      Trio.store.set('agents.list', data.agents || []);
      Trio.store.set('agents.loading', false);
      // Cleared on success, or a hub that recovers keeps telling the operator
      // managed agents are off until they reload the page.
      state.agentsError = null;
      state.agentsUnavailable = false;
      render(data.agents);
      if (state.view === 'roster') {
        const roster = document.getElementById('trio-roster-view');
        if (roster && !roster.hidden) renderPage(roster);
      }
    } catch (e) {
      console.warn(e);
      // Remember WHY the list is empty. A server without the agent supervisor
      // answers 409 for every agent call, and swallowing that left the roster
      // showing "No agents match — try another filter", which sends the
      // operator round the filters and the search box before they finally hit
      // "New agent" and get the real reason in a toast.
      state.agentsError = e;
      state.agentsUnavailable = e && e.status === 409;
      if (state.view === 'roster') {
        const roster = document.getElementById('trio-roster-view');
        if (roster && !roster.hidden) renderPage(roster);
      }
    }
  }
  function renderActivityEvent(e) {
    const time = e.ts ? new Date(e.ts).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' }) : '';
    const t = (e.type || 'event').toLowerCase();
    const meta = typeInfo[t] || { label: t, cls: '' };
    const content = typeof e.content === 'string' ? e.content : (e.message || (e.content ? JSON.stringify(e.content, null, 2) : ''));
    const body = content ? `<pre class="activity-raw">${esc(String(content).slice(0, 800))}</pre>` : '';
    return `<article class="activity-event ${esc(meta.cls)}"><time>${esc(time)}</time><b>${esc(meta.label)}</b>${body}</article>`;
  }
  function showActivity(id, events = [], offset = 0) {
    let panel = $('trio-activity');
    if (!panel) { panel = document.createElement('dialog'); panel.id = 'trio-activity'; panel.className = 'activity-panel'; document.body.append(panel); }
    Trio.ui.configureDialog(panel);
    const pageSize = 20;
    const page = events.slice(offset, offset + pageSize);
    if (!offset) panel.innerHTML = '<form method="dialog"><button type="submit" formnovalidate class="modal-close" aria-label="Close">×</button><h2>Agent activity</h2><div class="activity-list"></div></form>';
    const list = panel.querySelector('.activity-list');
    if (!page.length && !offset) { list.innerHTML = '<p class="home-empty">No activity.</p>'; }
    else { list.insertAdjacentHTML('beforeend', page.map(renderActivityEvent).join('')); }
    const existing = panel.querySelector('.activity-more');
    if (existing) existing.remove();
    if (offset + pageSize < events.length) {
      const more = document.createElement('button'); more.type = 'button'; more.className = 'activity-more'; more.textContent = 'Load more';
      more.addEventListener('click', () => showActivity(id, events, offset + pageSize));
      panel.querySelector('form').append(more);
    }
    panel.showModal();
  }
  async function activity(id) { try { const d = await Trio.api.get(`/api/agents/${encodeURIComponent(id)}/activity`); showActivity(id, d.events || []); } catch (e) { Trio.ui.toast(e.message); } }
  function init() { loadDiscovery(); refresh(); }
  function mount() { init(); }
  function unmount() { compactionPolls.forEach(timer => clearTimeout(timer)); compactionPolls.clear(); }
  async function create() {
    await loadDiscovery();
    const providers = state.providers.map(p => `<option value="${esc(p)}">${esc(p)}</option>`).join('');
    const defaultProvider = state.providers[0] || 'codex';
    const defaultModels = normalizeModels(state.agentModels[defaultProvider]);
    const models = modelOptions(state.agentModels[defaultProvider]);
    const defaultModelId = defaultModels[0]?.id || '';
    const initialEffort = lastEffort(defaultProvider, defaultModelId) || '';
    const createChanCodes = (state.channels || []).filter(c => !c.archived).map(c => c.code);
    const channelsField = createChanCodes.length
      ? `<fieldset class="field"><legend>Channels <span class="hint">optional — place later if blank</span></legend>${channelListMarkup(createChanCodes, new Set())}</fieldset>`
      : '';
    let channelApi = null;
    const effortControl = `<div id="effort-control">${effortSlider(effortsForModel(defaultProvider, defaultModelId), initialEffort, { defaultLabel: effortDefaultLabel(defaultProvider, defaultModelId), modelDefault: modelDefaultEffort(defaultProvider, defaultModelId) })}</div>`;
    const html = `<label class="field">Name <span class="hint">optional — assigned automatically if blank</span><input name="name" pattern="[A-Za-z0-9_]{1,32}" placeholder="Leave blank for a random character name"></label><label class="field">Provider <select name="provider">${providers}</select></label><label class="field">Model <select name="model">${models}</select></label>${effortControl}<label class="field">Working directory <input name="cwd" placeholder="/path/to/project"></label><label class="field">Permission profile ${permissionOptions()}</label>${channelsField}`;
    Trio.ui.modal('Create agent', html, async node => {
      const f = new FormData(node.querySelector('form'));
      const name = (f.get('name') || '').trim();
      const provider = f.get('provider');
      const model = f.get('model');
      const effort = f.get('effort') || '';
      const cwd = (f.get('cwd') || '').trim();
      const channels = channelApi ? [...channelApi.getSelected()] : [];
      if (!provider) { Trio.ui.toast('Provider is required'); return; }
      if (!model) { Trio.ui.toast('Model is required'); return; }
      // Explicit, not defaulted. The slider starts unset so it cannot silently
      // pick the lowest level for an agent nobody configured; refusing to
      // submit while it is unset is what turns that into a real choice rather
      // than an opaque "Default" whose behaviour nobody can state.
      if (!effort && effortsForModel(provider, model).length) {
        Trio.ui.toast('Choose a reasoning effort for this model'); return;
      }
      rememberEffort(provider, model, effort);
      const key = 'create:' + name;
      if (pendingAgentActions.has(key)) return;
      pendingAgentActions.add(key);
      Trio.ui.toast('Creating agent…');
      try {
        const result = await Trio.api.post('/api/agents', { name, provider, model, effort, cwd, permission_profile: f.get('permission_profile') || 'balanced', channels });
        await refresh();
        if (result?.agent?.id) Trio.workspace?.openDmByKey?.(result.agent.id);
      } catch (e) { Trio.ui.toast(e.message || 'Could not create agent'); }
      finally { pendingAgentActions.delete(key); }
    });
    const panel = document.getElementById('trio-control-modal');
    const providerField = panel?.querySelector('select[name="provider"]');
    const modelField = panel?.querySelector('select[name="model"]');
    const effortHost = panel?.querySelector('#effort-control');
    // Provider/model changes swap the available effort steps — rebuild the
    // slider. Preserve the user's current pick if the new model still supports
    // it (so flipping models to compare doesn't silently discard their choice);
    // otherwise fall back to the last effort picked for THAT provider+model.
    const rebuildEffort = () => {
      if (!effortHost) return;
      const p = providerField?.value, m = modelField?.value;
      const efforts = effortsForModel(p, m);
      const prev = effortHost.querySelector('input[name="effort"]')?.value ?? '';
      const keep = prev === '' || efforts.includes(prev) ? prev : (lastEffort(p, m) || '');
      effortHost.innerHTML = effortSlider(efforts, keep, { defaultLabel: effortDefaultLabel(p, m), modelDefault: modelDefaultEffort(p, m) });
      wireEffortSlider(effortHost);
    };
    if (effortHost) wireEffortSlider(effortHost);
    if (panel) channelApi = wireChannelList(panel);
    providerField?.addEventListener('change', () => {
      if (modelField) modelField.innerHTML = modelOptions(state.agentModels[providerField.value]);
      rebuildEffort();
    });
    modelField?.addEventListener('change', rebuildEffort);
  }
  Trio.agents = { init, mount, unmount, render, renderPage, refresh, loadDiscovery, normalizeModels, modelOptions, permissionOptions, viewModel, actionCaps, actionLabel, statusIcon, formatLastActive, action, create, effortsForModel, effortOptions, effortSlider, wireEffortSlider, lastEffort, rememberEffort, selection, toggleSelected, clearSelection, bulkAction, reportBulk, bulkAttributeJobs, showBulkAttributes, showBulkChannels, showBulkCompact };
})();
