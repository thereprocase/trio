(() => {
  'use strict';
  const Trio = window.Trio;
  const state = Trio.state;
  const api = Trio.api;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const listOf = value => Array.isArray(value) ? value : [];
  // state.agents is a flat array once 30-agents.js refresh() has run, but its
  // INITIAL store shape (01-store.js) is { list, selected, loading } — an object.
  // On cold boot, renderFacePile/detailMember run before that refresh, so read
  // agents defensively (an unguarded (state.agents||[]).map crashed workspace
  // mount → broke the rail/nav on a channel refresh).
  const agentArray = () => Array.isArray(state.agents) ? state.agents
    : (Array.isArray(state.agents?.list) ? state.agents.list : []);
  // Progressive Home load: each slice of the workspace refresh marks itself
  // ready as it lands, so Home paints instantly and each section fills in on
  // arrival (a spinner sits on whatever's still fetching — e.g. the slower
  // /api/agents) instead of one all-or-nothing "Loading workspace…" gate.
  const SPIN = '<span class="home-spinner" role="status" aria-label="Loading"></span>';
  const dataReady = k => !!(state.loaded && state.loaded[k]);
  function markLoaded(key) { (state.loaded = state.loaded || {})[key] = true; if (state.view === 'home') showView('home'); }
  const pendingDecisions = new Set();
  // #8: questions the operator dismissed from the attention inbox without
  // answering (they'll reply in the channel composer instead, or the ask is
  // moot). Persisted so a dismissal sticks across reloads — the question is
  // still unanswered server-side, so a fresh /api/questions would otherwise
  // re-surface it. Keyed by the attention-item id ('question-<id>').
  const DISMISSED_QUESTIONS_KEY = 'trio.dismissedQuestions';
  const dismissedQuestions = (() => {
    try { return new Set(JSON.parse(localStorage.getItem(DISMISSED_QUESTIONS_KEY) || '[]').map(String)); }
    catch { return new Set(); }
  })();
  function persistDismissedQuestions() {
    try { localStorage.setItem(DISMISSED_QUESTIONS_KEY, JSON.stringify([...dismissedQuestions])); }
    catch { /* storage full/disabled — dismissal stays in-memory for the session */ }
  }
  function dismissQuestion(itemId) { dismissedQuestions.add(String(itemId)); persistDismissedQuestions(); }
  function undismissQuestion(itemId) { dismissedQuestions.delete(String(itemId)); persistDismissedQuestions(); }
  function isQuestionDismissed(itemId) { return dismissedQuestions.has(String(itemId)); }
  // Questions actually visible in the attention inbox. The server already
  // excludes archived-channel questions, but state.questions can go stale (no
  // SSE; a channel archived by another client), so belt-and-suspenders drop any
  // whose channel is no longer in the active list — /api/channels enumerates
  // EVERY active channel for the (all-seeing) operator, so this can't false-hide
  // — plus any the operator dismissed. Only cross-check once the channel list
  // has loaded, so we don't hide everything on first paint.
  function visibleQuestions(src = state) {
    const chans = listOf(src.channels);
    const channelsLoaded = chans.length > 0;
    const activeCodes = new Set(chans.filter(c => !c.archived).map(c => c.code));
    return listOf(src.questions).filter(q => {
      if (dismissedQuestions.has('question-' + q.id)) return false;
      if (channelsLoaded && q.channel && !activeCodes.has(q.channel)) return false;
      return true;
    });
  }
  const $ = id => document.getElementById(id);
  // Monotonic navigation generation. Bumped in loadConversation so in-flight
  // DM loaders can detect that the user navigated away before their response
  // resolved, and bail before inserting private history into the wrong view.
  let navGen = 0;

  // ── stale threads ─────────────────────────────────────────────────────────
  // "Hide old threads": a channel or DM with no activity for N days drops into
  // a "show older" group rather than sitting at full weight in the sidebar
  // forever. This is a VIEW filter — nothing is archived, nothing is deleted,
  // and the group is one click away.
  //
  // Two things are never hidden, because hiding them would lose something the
  // operator has not dealt with:
  //   * anything UNREAD — an old thread you have not read is the single most
  //     likely thing to actually need you
  //   * whatever is open RIGHT NOW — a thread vanishing out of the sidebar
  //     while you are reading it reads as a bug
  function staleThreadDays() {
    const prefs = Trio.preferences?.read?.() || Trio.state.preferences || {};
    const n = Number(prefs.staleThreadDays);
    return Number.isFinite(n) && n > 0 ? n : 0;
  }
  function isStaleThread(item, openId) {
    const days = staleThreadDays();
    if (days <= 0) return false;
    if (Number(item.unread) > 0) return false;
    if (openId && (item.code === openId || item.key === openId)) return false;
    const t = new Date(item.last_at || 0).getTime();
    // No timestamp at all is NOT evidence of age — a channel created seconds
    // ago and never posted in has none. Treat unknown as fresh, so the filter
    // can only ever hide something it has positive evidence about.
    if (!t || isNaN(t)) return false;
    return t < Date.now() - days * 86400000;
  }
  function partitionStale(items, openId) {
    const fresh = [], stale = [];
    for (const item of items) (isStaleThread(item, openId) ? stale : fresh).push(item);
    return { fresh, stale };
  }
  function groupNavigation(channels = [], dms = {}) {
    const openId = state.dmKey || state.channel || '';
    const active = channels.filter(c => !c.archived);
    const chan = partitionStale(active, openId);
    const yours = partitionStale(dms.your_dms || [], openId);
    return {
      active: chan.fresh,
      staleChannels: chan.stale,
      archived: channels.filter(c => c.archived),
      yours: yours.fresh,
      staleDms: yours.stale,
      agentAudit: dms.agent_dms || [],
    };
  }
  const selectors = {
    pendingApprovals(src = state) { return listOf(src.approvals).filter(a => a.status !== 'resolved' && a.status !== 'accepted').length; },
    openTasks(src = state) { return listOf(src.tasks).filter(t => t.status === 'open' || t.status === 'blocked').length; },
    blockedTasks(src = state) { return listOf(src.tasks).filter(t => t.status === 'blocked').length; },
    blockedAgents(src = state) { return listOf(src.agents).filter(a => a.status === 'blocked' || a.status === 'error' || a.status === 'errored').length; },
    activeAgents(src = state) { return listOf(src.agents).filter(a => ['working','active','idle'].includes(a.status)).length; },
    unreadDms(src = state) { return (src.dms?.your_dms || []).reduce((s, d) => s + (Number(d.unread) || 0), 0); },
    unreadMentions(src = state) { return listOf(src.mentions).filter(m => !m.read).length; },
    pendingQuestions(src = state) { return visibleQuestions(src).length; },
    recentChannels(src = state) { return (src.channels || []).filter(c => !c.archived).slice(0, 5); },
    taskItems(src = state) {
      return listOf(src.tasks).map(t => ({
        id: t.id || t.task_id,
        status: t.status || 'open',
        title: t.description || t.message || t.title || 'Task',
        owner: t.claimed_by || '',
        blockers: Array.isArray(t.blocked_by) ? t.blocked_by : [],
        channel: t.channel,
        updatedAt: t.updated_at,
      }));
    },
    attention(src = state) { return selectors.pendingApprovals(src) + selectors.pendingQuestions(src); },
    attentionItems(src = state) {
      const items = [];
      for (const a of listOf(src.approvals)) {
        if (a.status === 'resolved' || a.status === 'accepted') continue;
        items.push({ id: a.id, kind: 'approval', severity: 'high', title: a.title || a.agent_name || 'Approval requested', source: a.agent_name || a.member_id, timestamp: a.created_at, status: a.status, body: a.reason || a.command || '', actions: ['accept','acceptForSession','decline', ...(a.can_cancel ? ['cancel'] : [])] });
      }
      for (const q of visibleQuestions(src)) {
        items.push({ id: 'question-' + q.id, kind: 'question', severity: 'medium', title: q.question || 'Question', source: q.member_name || q.member_id, timestamp: q.created_at, status: 'pending', body: q.content || '', channel: q.channel, actions: [...(q.channel ? ['openChannel'] : []), 'dismiss'] });
      }
      return items.sort((a, b) => new Date(b.timestamp || 0) - new Date(a.timestamp || 0));
    },
  };
  function attentionCount(meta = state.meta || {}) { return selectors.pendingApprovals({ approvals: meta.approvals }); }
  const channelSubtitle = readOnly => readOnly ? 'Archived channel — read only' : 'Live agent workspace';
  const channelMetadataSettled = () => !!(state.loaded?.meta && state.loaded?.agents);
  const channelMetadataReady = () => channelMetadataSettled()
    && !state.sliceErrors?.meta && !state.sliceErrors?.agents;
  function renderChannelMetadataState() {
    if (!state.channel || state.dmKey) return;
    const meta = document.getElementById('h-meta');
    if (meta) meta.textContent = state.readOnly
      ? channelSubtitle(true)
      : !channelMetadataSettled() ? 'Loading workspace…'
      : channelMetadataReady() ? channelSubtitle(false) : 'Workspace metadata unavailable';
  }
  // Set synchronously by a route that owns feed selection. Channel routes know
  // their feed immediately; DM/audit routes claim it before their asynchronous
  // metadata lookup so core cannot open the default channel in that gap. A
  // workspace mount failure never reaches onRoute and therefore makes no claim.
  let routeFeedClaimed = false;
  function openChannel(code, extra = '') {
    // Picking a destination dismisses the mobile drawer. Below 880px it is an
    // overlay lying on top of the conversation you just chose, so leaving it up
    // made every navigation end by hunting for the strip of scrim beside it.
    // Trio.nav no-ops when the drawer is already shut, which is the desktop case
    // and also every route-driven call that no human clicked.
    Trio.nav?.close?.();
    const readOnly = extra === 'archived';
    // The router is the SINGLE authority for loading a channel. navigate()
    // applies the route synchronously (03-router.js apply() invokes every
    // handler before returning), so onRoute() has already loaded by the time
    // this line is reached — loading again here cleared the message map and
    // re-seeded the unread watermark a second time, and tore down the
    // EventSource the first load had just opened, on every single channel
    // click. The direct call survives only as the no-router fallback.
    if (Trio.router?.navigate) Trio.router.navigate('channel', { code, archived: readOnly });
    else loadConversation(code, '#' + code, channelSubtitle(readOnly), readOnly, false);
  }
  // The operator's server-side read watermark for the conversation being
  // opened. Lives on their own roster row; absent until the roster lands.
  function operatorLastRead() {
    const opId = state.operator?.id || state.meta?.operator?.id;
    if (!opId) return 0;
    const row = state.members instanceof Map ? state.members.get(opId) : null;
    return Number(row?.last_read) || 0;
  }
  function loadConversation(channel, title, subtitle, readOnly = false, isDm = false, isAudit = false) {
    if (Trio.store) Trio.store.set('session.channel', channel);
    // Invalidate any in-flight DM loaders and cancel their fetches. Without
    // this, a late DM response resolved after navigating to a channel (or
    // another DM) would insert private history into the now-current view,
    // because the completion handler no longer sees a dmKey to filter against.
    navGen++;
    if (!isDm) Trio.loader?.cancelAll?.('dm:');
    state.view = 'conversation';
    showConversationPage();
    state.readOnly = !!readOnly;
    state.dmAudit = !!isAudit;
    state.dmKey = isDm ? (state.dmKey || '') : '';
    if (!isDm) state.dmRouteResolved = true;
    state.dmLoading = false;
    state.dmError = '';
    // Leaving a DM for a channel must also drop the DM's target identity —
    // otherwise buildSendPayload falls through to state.dmTargetId and stamps
    // a CHANNEL post with recipients=[last DM target], silently rescoping a
    // public message into a private DM (Sauron). Clear it with dmThread.
    if (!isDm) { state.dmThread = null; state.dmTargetId = ''; state.dmMemberIds = []; }
    state.channel = channel;
    renderRail();
    Trio.setChannelTitle(title);
    document.getElementById('h-meta').textContent = subtitle;
    if (!isDm) renderChannelMetadataState();
    renderFacePile();
    const detailsBtn = $('details-btn');
    const moreBtn = $('channel-more-btn');
    const searchBtn = $('search-btn');
    const conn = $('h-conn');
    if (detailsBtn) detailsBtn.classList.remove('hidden');
    if (moreBtn) moreBtn.classList.remove('hidden');
    if (searchBtn) searchBtn.classList.remove('hidden');
    if (conn) conn.classList.remove('hidden');
    const banner = document.getElementById('private-banner');
    if (banner) { banner.classList.toggle('hidden', !isDm); banner.classList.toggle('audit', !!isAudit); banner.textContent = isDm ? (isAudit ? 'Agent-to-agent audit — read only' : readOnly ? 'Archived private conversation — read only' : 'Private conversation') : ''; }
    state.messages = new Map(); state.messageDomById = new Map(); state.answers = new Map();
    // Freeze this conversation's unread divider BEFORE its history arrives.
    // The prime burst calls markRead() on every message while the view sits at
    // the bottom, so anything computed after it would already be caught up —
    // which is why the divider never used to appear on entry. The operator's
    // own last_read comes off the roster; if the roster is not in yet the base
    // stays 0 and no divider is drawn, which is the honest answer.
    Trio.conversation?.seedWatermark?.(operatorLastRead());
    Trio.conversation?.render?.();
    Trio.composer?.syncReadOnly?.();
    // Swap the composer to THIS conversation's own draft/targets/images now that
    // channel + dmKey are final. openChannel fires the router before this point,
    // so the router hook alone would load stale state (Bug C).
    Trio.composer?.refresh?.();
    if (Trio.startEvents) { Trio.startEvents(state.channel); routeFeedClaimed = true; }
    // If the details drawer is open, re-render it for the conversation we just
    // switched to. Otherwise it keeps the previous conversation's topic /
    // members / size row — e.g. a stale "Conversation size" (or, before this,
    // "not available for DMs") left over from a DM after moving to a channel.
    if ($('channel-drawer')?.classList.contains('open')) showDetails(true);
  }
  function openDm(dm, readOnly = false, audit = false) {
    Trio.nav?.close?.();
    const auditReadOnly = !!audit;
    state.dmRouteResolved = true;
    state.dmMemberIds = (dm.member_ids || []).slice();
    state.dmTargetId = state.dmMemberIds[0] || '';
    state.dmName = dm.name || dm.key;
    state.dmKey = dm.key;
    state.dmThread = dm;
    loadConversation(dm.channel || state.channel, 'DM ' + state.dmName, auditReadOnly ? 'Agent-to-agent audit' : readOnly ? 'Archived private conversation' : 'Private conversation', readOnly || auditReadOnly, true, auditReadOnly);
    const route = Trio.router?.current?.();
    const routeAlreadyExact = route?.name === (auditReadOnly ? 'audit' : 'dm')
      && route?.params?.key === dm.key;
    if (Trio.router?.navigate && !routeAlreadyExact) Trio.router.navigate(auditReadOnly ? 'audit' : 'dm', { key: dm.key, ...(readOnly && !auditReadOnly ? { archived: true } : {}) });
    Trio.loader?.cancel?.('dm:' + dm.key);
    state.dmLoading = true; state.dmError = ''; Trio.conversation?.render?.();
    // Capture the navigation generation and dm key after loadConversation so
    // the completion handler can reject a stale response (user navigated to a
    // channel or a different DM before this resolved).
    const gen = navGen;
    const dmKey = dm.key;
    const loader = Trio.loader?.load ? Trio.loader : { load: (name, fn) => { const c = { abort() {} }; return fn(c); } };
    loader.load('dm:' + dm.key, signal => api.get('/api/dms?with=' + encodeURIComponent(dm.key) + (readOnly && !auditReadOnly ? '&archived=1' : ''), false, { signal })).then(data => {
      if (gen !== navGen || state.dmKey !== dmKey) return;
      state.dmLoading = false; state.dmError = '';
      if (data && Array.isArray(data.messages)) { data.messages.forEach(Trio.conversation.upsert); }
      if (data && data.ok === false) { state.dmError = data.error || 'Could not load DM'; }
      Trio.conversation?.render?.();
    }).catch(error => {
      if (gen !== navGen) return;
      if (error?.name === 'AbortError' || (typeof error === 'string' && error.includes('aborted'))) return;
      state.dmLoading = false; state.dmError = error.message || 'Could not load DM'; Trio.conversation?.render?.();
    });
  }
  function openDmByKey(key, audit = false, routeOwned = false, archived = false) {
    if (!key) return Promise.resolve(false);
    const gen = navGen;
    const firstPath = archived
      ? '/api/dms?archived=1&with=' + encodeURIComponent(key)
      : '/api/dms?with=' + encodeURIComponent(key);
    return api.get(firstPath).then(data => {
      if (gen !== navGen) return null;
      if (archived) {
        const dm = (data.your_dms || []).find(d => d.key === key);
        if (dm) return openDm(dm, true, false);
        throw new Error('Archived conversation not found');
      }
      const auditThread = (data.agent_dms || []).find(d => d.key === key);
      // An audit route must never degrade into an operator DM with the same
      // key: that would turn a declared read-only view into a writable one.
      if (audit) {
        if (auditThread) return openDm(auditThread, false, true);
        throw new Error('Audit conversation not found');
      }
      const yours = (data.your_dms || []).find(d => d.key === key);
      if (yours) return openDm(yours, false, false);
      if (auditThread) return openDm(auditThread, false, true);
      return api.get('/api/dms?archived=1&with=' + encodeURIComponent(key));
    }).then(data => {
      if (gen !== navGen) return;
      if (!data) return;
      const dm = (data.your_dms || []).find(d => d.key === key);
      if (dm) return openDm(dm, true);
      throw new Error('Conversation not found');
    }).catch(error => {
      if (gen !== navGen) return;
      const message = error.message || 'Could not load DM';
      if (routeOwned) {
        state.dmLoading = false;
        state.dmError = message;
        Trio.conversation?.render?.();
        Trio.composer?.syncReadOnly?.();
      } else Trio.ui.toast(message);
    });
  }
  function beginDmRoute(key, audit, archived = false) {
    // Claim before the first await and close any previous channel feed. A
    // failed lookup then remains visibly offline with an inline DM error,
    // rather than silently continuing to show the previous/default channel.
    routeFeedClaimed = true;
    navGen++;
    Trio.loader?.cancelAll?.('dm:');
    Trio.stopEvents?.();
    if (Trio.store) Trio.store.set('session.channel', '');
    state.view = 'conversation';
    showConversationPage();
    state.channel = '';
    state.members = new Map();
    state.readOnly = !!audit || !!archived;
    state.dmAudit = !!audit;
    state.dmRouteResolved = false;
    state.dmKey = key;
    state.dmName = key;
    state.dmThread = null;
    state.dmTargetId = '';
    state.dmMemberIds = [];
    state.dmLoading = true;
    state.dmError = '';
    state.messages = new Map();
    state.messageDomById = new Map();
    state.answers = new Map();
    Trio.setChannelTitle('DM ' + key);
    document.getElementById('h-meta').textContent = audit ? 'Agent-to-agent audit'
      : archived ? 'Archived private conversation' : 'Private conversation';
    const banner = document.getElementById('private-banner');
    if (banner) {
      banner.classList.remove('hidden');
      banner.classList.toggle('audit', !!audit);
      banner.textContent = audit ? 'Agent-to-agent audit — read only'
        : archived ? 'Archived private conversation — read only' : 'Private conversation';
    }
    renderFacePile();
    Trio.conversation?.render?.();
    // Load this DM's own draft (or an empty one) so text from the previous
    // public channel cannot remain visible in a box labelled private. refresh
    // also applies the unresolved-route read-only gate.
    Trio.composer?.refresh?.();
  }
  const toast = (m, timeout, action) => Trio.ui.toast(m, timeout, action);
  const modal = (t, b, s) => Trio.ui.modal(t, b, s);
  async function archive(kind, key, archived) {
    try {
      await api.post('/api/archives', {kind, key, archived});
      // Optimistically drop the archived channel's questions so the attention
      // inbox clears instantly and survives a raced/failed questions refetch
      // (Promise.allSettled swallows a failed /api/questions in refresh()).
      if (kind === 'channel' && archived) state.questions = listOf(state.questions).filter(q => q.channel !== key);
      await refresh(); Trio.ui.toast(archived ? 'Archived' : 'Restored');
    }
    catch (error) { Trio.ui.toast(error.message || 'Could not update archive'); }
  }
  async function resolveApproval(id, decision) {
    if (!id || pendingDecisions.has(id + ':' + decision)) return;
    pendingDecisions.add(id + ':' + decision);
    try {
      const url = decision === 'cancel' ? '/api/approvals/' + encodeURIComponent(id) + '/cancel' : '/api/approvals/' + encodeURIComponent(id) + '/resolve';
      await api.post(url, { decision });
      const list = listOf(state.approvals);
      const a = list.find(x => x.id === id);
      if (a) {
        a.status = (decision === 'accept' || decision === 'acceptForSession') ? 'accepted' : 'resolved';
        a.resolved_decision = decision;
      }
      showView('attention');
      await Trio.workspace?.refresh?.();
    } catch (error) { Trio.ui.toast(error.message || 'Could not resolve approval'); }
    finally { pendingDecisions.delete(id + ':' + decision); }
  }
  function navIcon(name) {
    const icons = {
      home: '<path d="M4 11.5 12 4l8 7.5"/><path d="M6 10v9a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1v-9"/>',
      attention: '<path d="M4 13h4l2 3h4l2-3h4"/><path d="M5 13 7 5h10l2 8v5a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1Z"/>',
      tasks: '<path d="M9 6h11M9 12h11M9 18h11"/><path d="m4 6 1 1 2-2M4 12l1 1 2-2M4 18l1 1 2-2"/>',
      messages: '<path d="M22 7L12 13 2 7"/><path d="M2 7v10h20V7z"/>',
      plus: '<path d="M12 5v14M5 12h14"/>',
      roster: '<circle cx="9" cy="8" r="3"/><path d="M3 20a6 6 0 0 1 12 0M16 6a3 3 0 0 1 0 6M21 20a5 5 0 0 0-4-4.9"/>',
      archive: '<path d="M3 8h18v3H3z"/><path d="M5 11v9h14v-9"/><path d="M10 15h4"/>',
      database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
      edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
      x: '<path d="M18 6 6 18M6 6l12 12"/>',
      settings: '<circle cx="12" cy="12" r="3"/><path d="M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.319 1.915 2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 4.033 2.34 2.34 0 0 0-3.319 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831A2.34 2.34 0 0 1 6.35 6.051a2.34 2.34 0 0 0 3.319-1.915"/>'
    };
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || ''}</svg>`;
  }
  function initials(label) { return String(label || '?').split(/\s+/).map(part => part[0]).join('').slice(0, 2).toUpperCase(); }
  function agentRecord(idOrName) {
    const agents = Trio.store?.get('agents.list') || state.agents || [];
    return agents.find(agent => agent.id === idOrName || agent.name === idOrName) || null;
  }
  function avatar(label, tone = 'eucalyptus', status = '', avatarUrl = '') {
    const content = avatarUrl
      ? `<img src="${esc(avatarUrl)}" alt="" class="avatar-svg-image">`
      : esc(initials(label));
    return `<span class="av av-28 tone-${tone} ${avatarUrl ? 'avatar-svg' : ''} ${status ? 'st-' + status : ''}">${content}${status ? '<span class="st-ring"></span>' : ''}</span>`;
  }
  function avatarFor(memberOrName, status = '') {
    const member = typeof memberOrName === 'object' ? memberOrName : null;
    const label = member?.name || member?.id || memberOrName || '?';
    const agent = agentRecord(member?.id || label);
    return avatar(label, avatarTone(label), status, member?.avatar_url || agent?.avatar_url || '');
  }
  // Shared with 11-conversation.js via Trio.avatarTone (00-core.js) — see its
  // comment there for why this needed to stop being a bare per-label hash.
  const avatarTone = Trio.avatarTone;
  // ── face pile ─────────────────────────────────────────────────────────────
  // The pile answers "who is in this room RIGHT NOW". That contract was
  // written here long before it was implemented: the code filtered only
  // `archived`, sliced four records off an unclassified roster, and then
  // reported `+N` across every member the channel had ever held. A room with
  // three live agents and eight departed ones therefore showed four faces and
  // "+7", and neither number was true.
  //
  // The fix is an ordering one: liveness has to decide WHO is chosen, not just
  // how their dot is painted, so classification moves ahead of the slice.
  //
  // Present is an explicit allow-list rather than "everything except offline".
  // channelStatus() already collapses anything it does not recognise to
  // 'offline', so an exclusion list would silently admit a future status that
  // nobody decided about; this way adding one forces the choice. `sleeping`
  // and `errored` are deliberately absent — channelStatus only returns those
  // on its non-live branch, so they mean "not here", and an agent that is
  // sleeping but still heartbeating reaches us as `idle` from member_status()
  // anyway. The drawer remains the place to find everyone.
  // Order is the rank. One relation here comes from the server and the rest is
  // a judgement call, so they are worth separating: member_status() explicitly
  // ranks `blocked` above `working`, because a blocked session looks busy from
  // outside but sits there until a human answers it — that is the costliest
  // face to lose behind a "+N". The placement of `compacting` is ours; it never
  // passes through member_status() at all (it is supervisor-only), and it sits
  // above active/idle because it is a busy state the operator may be waiting on.
  const PRESENT_STATUSES = ['blocked', 'working', 'compacting', 'active', 'idle'];
  // Index doubles as rank: the cap sheds the least informative faces rather
  // than an arbitrary tail. Sort is stable, so equal ranks keep roster order.
  const PRESENCE_RANK = new Map(PRESENT_STATUSES.map((status, i) => [status, i]));
  // A no-signal record is deliberately NOT given the benefit of the doubt here,
  // unlike the stale-thread filter above which treats a missing timestamp as
  // fresh. The two differ because the evidence differs: a channel genuinely may
  // carry no last_at, whereas the roster boundary always supplies a status
  // (nth_web.py's member_status returns one of blocked/working/active/idle/
  // stale/dead, and a member with no heartbeat at all reads `dead`). The only
  // status-free record the client ever holds is the operator's own
  // {id,name,source,pending}, which is forced to `active` below. So an
  // unclassifiable member here is a client-state bug, and painting it as a
  // present participant with an offline dot would be the pile contradicting
  // itself — exactly the failure this change exists to remove.
  // How many faces the header can afford, which is not a constant: at 360px
  // three faces leave only about 40px for the title. Two faces plus a badge is
  // the most the narrow header can spend and still say which room you are in,
  // and the badge keeps the count honest at either width. 880px is the header's
  // own mobile breakpoint, so JS and CSS cannot disagree about the layout.
  const FACE_LIMIT_WIDE = 4;
  const FACE_LIMIT_NARROW = 2;
  const FACE_MEDIA = '(max-width: 880px)';
  const faceLimit = () => {
    try { return window.matchMedia?.(FACE_MEDIA)?.matches ? FACE_LIMIT_NARROW : FACE_LIMIT_WIDE; }
    catch { return FACE_LIMIT_WIDE; }
  };
  // Pure, and separated from the DOM on purpose: tests/dom-harness.js stubs
  // matchMedia to a permanent { matches: false }, so any limit read from a
  // media query in here would make a "mobile" assertion silently exercise the
  // desktop branch and pass. The limit is an argument so a regression can set
  // it directly.
  function facePileModel(members, agents, operator, limit = FACE_LIMIT_WIDE) {
    const agentsById = new Map(listOf(agents).map(agent => [agent.id, agent]));
    const present = listOf(members).map(member => {
      // Merge the supervisor's {live,busy,state} over the roster member — the
      // same source the channel drawer uses — so the pile agrees with the
      // drawer instead of reading only the heartbeat-based roster status.
      const agent = agentsById.get(member.id);
      const merged = agent ? { ...member, ...agent } : member;
      // The operator viewing this dashboard is present by definition, and
      // their bare {id,name,source} shape carries no status/live/state for
      // channelStatus to read — it normalises to 'offline'. Classifying before
      // the slice means this special case has to move here too, or the one
      // participant guaranteed to be in the room is the first one dropped.
      const status = (operator?.id && member.id === operator.id) ? 'active' : channelStatus(merged);
      return { member: merged, status };
    }).filter(row => PRESENCE_RANK.has(row.status));
    present.sort((a, b) => PRESENCE_RANK.get(a.status) - PRESENCE_RANK.get(b.status));
    const cap = Math.max(1, Number(limit) || 1);
    return { faces: present.slice(0, cap), overflow: Math.max(0, present.length - cap) };
  }
  function renderFacePile() {
    const pile = $('face-pile'); if (!pile) return;
    if (!state.channel) { pile.classList.add('hidden'); return; }
    // The first route render can have roster names but not the slower agent
    // metadata that supplies stable avatar URLs and supervisor truth. Hiding
    // the pile until both slices settle prevents duplicate fallback initials
    // from presenting as final identity (the cold-phone screenshot bug).
    if (!channelMetadataReady()) { pile.replaceChildren(); pile.classList.add('hidden'); return; }
    pile.classList.remove('hidden');
    pile.replaceChildren();
    const allMembers = [...(state.members?.values?.() || [])];
    // For a DM, show only conversation participants — not the whole inbox
    // channel roster (which lists every agent ever created).
    const members = (state.dmKey && Array.isArray(state.dmMemberIds) && state.dmMemberIds.length
      ? allMembers.filter(m => state.dmMemberIds.includes(m.id))
      : allMembers).filter(m => !m.archived);
    const operator = state.operator || state.meta?.operator;
    if (operator?.id && !members.some(member => member.id === operator.id)) members.push(operator);
    const { faces, overflow } = facePileModel(members, agentArray(), operator, faceLimit());
    faces.forEach(({ member, status }) => {
      const node = document.createElement('span');
      node.innerHTML = avatarFor(member, status);
      const face = node.firstElementChild;
      const label = member.name || member.id || 'Channel member';
      face.setAttribute('aria-label', label + (status === 'working' ? ' — actively working' : ''));
      face.title = label + toolSuffix(member, status);
      pile.append(face);
    });
    if (overflow) {
      const more = document.createElement('span');
      // Counts only members the pile is about — the ones actually here. The
      // old count included every departed record, which is what made "+7"
      // read as seven more agents in a room that had three.
      more.className = 'more'; more.textContent = '+' + overflow;
      more.setAttribute('aria-label', `${overflow} more here`);
      pile.append(more);
    }
    pile.classList.toggle('hidden', !faces.length);
  }
  function navigateView(view) {
    Trio.nav?.close?.();
    const route = { home: 'home', attention: 'attention', messages: 'messages', tasks: 'tasks', roster: 'roster', prefs: 'prefs', archive: 'archive', data: 'data' }[view] || 'home';
    if (Trio.router?.navigate) Trio.router.navigate(route);
    else showView(view);
  }
  // `badge` is a numeric count (mentions/attention). `dot` renders a plain
  // unread dot when there's no count — the low-signal "there are unread
  // messages here" indicator. A count wins over a dot when both apply.
  function navItem(label, icon, onClick, badge = '', active = false, dot = false) {
    const button = document.createElement('button'); button.type = 'button'; button.className = 'nav-item'; button.classList.toggle('active', active);
    button.setAttribute('data-tip', label);
    const meta = badge
      ? `<span class="nav-meta"><span class="badge">${esc(badge)}</span></span>`
      : dot ? '<span class="nav-meta"><span class="unread-dot" aria-label="Unread"></span></span>' : '';
    button.innerHTML = `<span class="nav-hash">${icon === 'hash' ? '#' : navIcon(icon)}</span><span class="nav-label">${esc(label)}</span>${meta}`;
    button.addEventListener('click', onClick); return button;
  }
  function section(title, items, add, onAdd = createChannel, addLabel = 'Create channel', emptyText = '') {
    const wrap = document.createElement('section'); wrap.className = 'nav-section';
    const head = document.createElement('div'); head.className = 'nav-head'; head.innerHTML = `<h3>${esc(title)}</h3>`;
    if (add) { const button = document.createElement('button'); button.type = 'button'; button.className = 'add-btn'; button.setAttribute('aria-label', addLabel); button.title = addLabel; button.innerHTML = navIcon('plus'); button.addEventListener('click', onAdd); head.append(button); }
    wrap.append(head);
    // An empty section used to render as a bare heading, so genuine emptiness
    // was indistinguishable from a rendering bug — and on first run the entire
    // sidebar was three headings and a 16px "+" with no words at all. Say that
    // it is empty, and let the caller word it.
    if (!items.length && emptyText) {
      const empty = document.createElement('p');
      empty.className = 'nav-empty';
      empty.textContent = emptyText;
      wrap.append(empty);
    }
    items.forEach(item => wrap.append(item)); return wrap;
  }
  function staleToggle(group, count) {
    const btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'nav-stale-toggle';
    const open = !!state.staleOpen?.[group];
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    // Say the NUMBER, not just "older". A bare "show older" gives no reason to
    // click and no sense of what the preference is doing on your behalf.
    btn.textContent = open
      ? 'Hide ' + count + ' older'
      : 'Show ' + count + ' older' + (group === 'dms' ? ' conversation' : ' channel') + (count === 1 ? '' : 's');
    btn.addEventListener('click', () => {
      state.staleOpen = state.staleOpen || {};
      state.staleOpen[group] = !state.staleOpen[group];
      renderRail();
    });
    return btn;
  }
  function itemWithAdd(item, onAdd, addLabel) {
    const row = document.createElement('div'); row.className = 'nav-item-row';
    const button = document.createElement('button'); button.type = 'button'; button.className = 'add-btn'; button.setAttribute('aria-label', addLabel); button.title = addLabel; button.innerHTML = navIcon('plus'); button.addEventListener('click', onAdd);
    row.append(item, button); return row;
  }
  function dmItem(dm, audit = false) {
    const label = dm.name || dm.key || 'Conversation';
    const people = label.split(/\s*[↔·,]\s*/).filter(Boolean);
    const visual = audit && people.length > 1 ? `<span class="dm-pair">${avatarFor(people[0])}${avatarFor(people[1])}</span>` : avatarFor(people[0] || label, dm.unread ? 'online' : 'idle');
    const button = document.createElement('button'); button.type = 'button'; button.className = 'dm-item'; button.classList.toggle('active', state.view === 'conversation' && state.dmKey === dm.key);
    button.setAttribute('data-tip', label);
    button.innerHTML = `${visual}<span class="dm-copy"><span class="dm-name">${esc(label)}</span></span>${dm.unread ? '<span class="unread-dot" aria-label="Unread"></span>' : ''}`;
    button.addEventListener('click', () => openDm(dm, false, audit)); return button;
  }
  function renderRail() {
    const rail = $('workspace-rail'); if (!rail) return;
    const nav = groupNavigation(state.channels || [], state.dms || {});
    rail.textContent = '';
    const workspaceItems = [
      navItem('Home', 'home', () => navigateView('home'), '', state.view === 'home'),
      navItem('Attention', 'attention', () => navigateView('attention'), String(selectors.attention() || ''), state.view === 'attention'),
      navItem('Messages', 'messages', () => navigateView('messages'), String(selectors.unreadDms() + selectors.unreadMentions() || ''), state.view === 'messages'),
      itemWithAdd(navItem('Agent roster', 'roster', () => navigateView('roster'), '', state.view === 'roster'), () => Trio.agents?.create?.(), 'Create agent'),
      navItem('Tasks', 'tasks', () => navigateView('tasks'), '', state.view === 'tasks'),
      // Preferences moved off the rail into the account menu (sidebar name/avatar).
    ];
    // Number badge = unread @mentions (the loud signal); a plain dot = any
    // other unread messages (normal replies). So a channel with unread
    // non-mention traffic still shows *where* it is, without a nagging count.
    const channelNav = c => navItem(c.code, 'hash', () => openChannel(c.code), c.unread_mentions || '', state.view === 'conversation' && !state.dmKey && state.channel === c.code, (c.unread || 0) > 0);
    const channelItems = nav.active.map(channelNav);
    if (!channelItems.length && state.workspaceLoading) { const loading = document.createElement('div'); loading.className = 'nav-loading'; loading.textContent = 'Loading channels…'; channelItems.push(loading); }
    // The stale group is expanded per-section and NOT persisted: it is a
    // "let me look" gesture, not a setting. Persisting it would quietly undo
    // the preference the operator set, one session at a time.
    if (nav.staleChannels.length) {
      channelItems.push(staleToggle('channels', nav.staleChannels.length));
      if (state.staleOpen?.channels) nav.staleChannels.forEach(c => channelItems.push(channelNav(c)));
    }
    const dmItems = nav.yours.map(d => dmItem(d));
    if (nav.staleDms.length) {
      dmItems.push(staleToggle('dms', nav.staleDms.length));
      if (state.staleOpen?.dms) nav.staleDms.forEach(d => dmItems.push(dmItem(d)));
    }
    rail.append(section('Workspace', workspaceItems));
    rail.append(section('Channels', channelItems, true, createChannel, 'Create channel',
                        'No channels yet — use + to make one.'));
    rail.append(section('Direct Messages', dmItems, true, openDmDialog, 'Start direct message',
                        'No direct messages yet.'));
    rail.append(section('Agent-to-Agent', nav.agentAudit.map(d => dmItem(d, true)), false, createChannel, 'Create channel',
                        'No agent-to-agent threads yet.'));
    const operator = state.operator || state.meta?.operator || {}; const opName = operator.name || 'Workspace'; const opAvatar = $('operator-avatar'); const opLabel = $('operator-name'); const opRole = $('operator-role');
    if (opAvatar) { opAvatar.textContent = initials(opName); opAvatar.className = 'operator-avatar tone-' + avatarTone(opName); }
    if (opLabel) opLabel.textContent = opName; if (opRole) opRole.textContent = operator.name ? 'Workspace owner' : 'Live agent coordination';
  }
  function updateTopbar(title, subtitle) {
    Trio.setChannelTitle(title);
    const m = $('h-meta');
    if (m) m.textContent = subtitle || '';
  }
  function showConversationPage() {
    const shell = document.querySelector('.conversation-shell');
    shell?.classList.remove('workspace-page');
    document.querySelectorAll('[data-trio-view]').forEach(panel => { panel.hidden = true; });
  }
  function viewHeader(title, subtitle, action) {
    const header = document.createElement('div'); header.className = 'view-hero';
    // Title + subtitle live in their own block so an optional action button can
    // sit beside them (flex row) without turning the h2/p into flex siblings.
    // The .view-hero descendant CSS still matches, so the no-action case is
    // visually identical to before.
    const main = document.createElement('div'); main.className = 'view-hero-main';
    main.innerHTML = `<h2>${esc(title)}</h2><p>${esc(subtitle)}</p>`;
    header.append(main);
    if (action) {
      header.classList.add('view-hero-row');
      const b = document.createElement('button'); b.type = 'button';
      b.className = 'btn primary view-hero-action';
      b.innerHTML = `${action.icon || ''}<span>${esc(action.label)}</span>`;
      b.addEventListener('click', action.onClick);
      header.append(b);
    }
    return header;
  }
  async function markMessagesRead(ids, read = true) {
    if (!ids || !ids.length) return;
    try {
      await api.post('/api/messages/mark-read', { ids, read });
    } catch (error) { console.warn('mark read failed', error); }
  }
  function statusLabel(agent) {
    const status = String(agent.status || agent.state || (agent.busy ? 'working' : agent.live ? 'active' : 'offline')).toLowerCase();
    return status === 'working' || status === 'active' ? status : status === 'error' || status === 'errored' ? 'errored' : status;
  }
  function renderHome(panel) {
    panel.replaceChildren();
    // Gated on there being NOTHING TO SHOW, not on the loading flags. refresh()
    // marks every slice loaded once it settles — deliberately, so a partially
    // failed refresh cannot leave sections spinning forever — which made the
    // old `!dataReady('channels')` permanently false and this whole branch
    // unreachable. With every request failing, Home therefore rendered a
    // cheerful greeting, three zeros and a green health row, and the only
    // trace was a console.warn nobody has open.
    if (state.workspaceError && !listOf(state.channels).length) { const p = document.createElement('p'); p.className = 'home-empty'; p.textContent = state.workspaceError; const b = document.createElement('button'); b.type = 'button'; b.className = 'btn primary'; b.textContent = 'Retry'; b.addEventListener('click', refresh); p.append(b); panel.append(p); return; }
    const operatorName = state.operator?.name || state.meta?.operator?.name || 'there';
    const intro = document.createElement('div'); intro.className = 'hello';
    intro.innerHTML = `<div class="greet">Good ${new Date().getHours() < 12 ? 'morning' : new Date().getHours() < 18 ? 'afternoon' : 'evening'}, ${esc(operatorName)}.</div><div class="sub">Here’s what’s happening across your workspace.</div>`;
    const grid = document.createElement('div'); grid.className = 'home-grid';
    const cards = [
      { title: 'Attention inbox', ready: dataReady('approvals') && dataReady('questions'), count: selectors.attention(), subtitle: 'Need a decision', tone: 'warn', detail: `${selectors.pendingApprovals()} approvals · ${selectors.pendingQuestions()} questions`, action: () => navigateView('attention') },
      { title: 'Messages for you', ready: dataReady('dms') && dataReady('mentions'), count: selectors.unreadDms() + selectors.unreadMentions(), subtitle: 'Unread & mentions', tone: 'accent', detail: `${selectors.unreadDms()} DMs · ${selectors.unreadMentions()} mentions`, action: () => navigateView('messages') },
      { title: 'Tasks in flight', ready: dataReady('tasks'), count: selectors.openTasks(), subtitle: 'Across every channel', tone: 'ok', detail: `${listOf(state.tasks).filter(t => t.status === 'claimed').length} claimed · ${listOf(state.tasks).filter(t => t.status === 'blocked').length} blocked`, action: () => navigateView('tasks') },
    ];
    for (const { title, count, subtitle, tone, detail, action, ready } of cards) {
      const card = document.createElement('button'); card.type = 'button'; card.className = 'hcard';
      card.innerHTML = `<div class="hc-top"><span class="hc-ic ${tone}"><span aria-hidden="true">${tone === 'warn' ? '!' : tone === 'ok' ? '✓' : '✦'}</span></span><span><span class="hc-title">${esc(title)}</span><span class="hc-sub">${esc(subtitle)}</span></span></div><span class="hc-num">${ready ? esc(String(count)) : SPIN}</span><span class="hc-sub">${ready ? esc(detail) : 'Loading…'}</span>`;
      card.addEventListener('click', action); grid.append(card);
    }
    // Agents are the slowest slice (/api/agents busy-probing), so this section
    // spins on its own while the rest of Home is already interactive.
    const agentsReady = dataReady('agents');
    const agents = agentArray().filter(a => ['working','active'].includes(statusLabel(a))).slice(0, 4);
    const working = document.createElement('section'); working.className = 'home-section';
    working.innerHTML = `<div class="sec-head"><h3>Working right now</h3><span class="count">${agentsReady ? agents.length : SPIN}</span><span class="sh-line"></span></div>`;
    const workingList = document.createElement('div'); workingList.className = 'home-agent-list';
    if (!agentsReady) { workingList.innerHTML = `<div class="home-loading">${SPIN}<span>Loading agents…</span></div>`; }
    else if (!agents.length) { const p = document.createElement('p'); p.className = 'home-empty'; p.textContent = 'No agents are active right now.'; workingList.append(p); }
    else agents.forEach(agent => { const row = document.createElement('div'); row.className = 'hc-row'; row.innerHTML = `<span class="dotm" style="background:var(--ok)"></span><span class="grow"><b>${esc(agent.name || agent.id || 'Agent')}</b> · ${esc(agent.status_text || agent.status || 'Active')}</span><span class="t">${esc(agent.provider || 'agent')}</span>`; workingList.append(row); });
    working.append(workingList);
    const recent = document.createElement('section'); recent.className = 'home-section';
    recent.innerHTML = `<div class="sec-head"><h3>Recently active channels</h3><span class="sh-line"></span></div>`;
    const recentList = document.createElement('div'); recentList.className = 'home-channel-list';
    if (!dataReady('channels')) { recentList.innerHTML = `<div class="home-loading">${SPIN}<span>Loading channels…</span></div>`; }
    else {
      const chans = selectors.recentChannels();
      // A brand new operator's only route to a first channel used to be a bare
      // "+" glyph in the sidebar, and this line was a flat full stop. Put the
      // action in the empty state.
      if (!chans.length) {
        const p = document.createElement('p'); p.className = 'home-empty';
        p.textContent = 'No active channels yet. ';
        const b = document.createElement('button');
        b.type = 'button'; b.className = 'btn primary';
        b.textContent = 'Create your first channel';
        b.addEventListener('click', createChannel);
        p.append(b);
        recentList.append(p);
      }
      for (const c of chans) { const b = document.createElement('button'); b.type = 'button'; b.className = 'home-channel'; b.innerHTML = `<strong>#${esc(c.code)}</strong><span>${esc(c.topic || 'No topic')}</span><small>${esc(String(c.members?.length || 0))} members${c.unread ? ` · ${esc(String(c.unread))} unread` : ''}</small>`; b.addEventListener('click', () => openChannel(c.code)); recentList.append(b); }
    }
    recent.append(recentList);
    const usage = document.createElement('section'); usage.className = 'home-section'; usage.innerHTML = '<div class="sec-head"><h3>Usage</h3><span class="sh-line"></span></div>';
    if (!dataReady('usage')) { const d = document.createElement('div'); d.className = 'home-loading'; d.innerHTML = `${SPIN}<span>Loading usage…</span>`; usage.append(d); } else usage.append(usageMeters(state.usage));
    const health = document.createElement('section'); health.className = 'home-section'; health.innerHTML = '<div class="sec-head"><h3>Runtime health</h3><span class="sh-line"></span></div>';
    const healthRow = document.createElement('div'); healthRow.className = 'health-row';
    // Derived, not decorative. These three chips were hardcoded to the tone
    // 'ok' — the string, in all three cases, never computed from anything — so
    // the row was structurally incapable of showing bad health and stayed
    // green while every request behind it was failing. A health panel that
    // cannot report ill health is worse than none: it actively argues the
    // operator out of investigating.
    //
    // What each one can honestly claim:
    //   Hub       — the SSE connection state, which is the live signal.
    //   Agents    — 'warn' when the roster could not be fetched at all, since
    //               "0 connected" and "could not ask" look identical otherwise.
    //   Database  — the server answers /api/channels out of SQLite, so a
    //               successful channels slice is the evidence it is readable.
    const connState = (Trio.store?.get?.('connection') || {});
    const hubTone = connState.failed ? 'warn' : 'ok';
    const hubValue = connState.text || 'Live';
    const agentsFailed = !!state.sliceErrors?.agents;
    const dbFailed = !!state.sliceErrors?.channels;
    [['Hub', hubTone, hubValue],
     ['Agents', agentsFailed ? 'warn' : 'ok',
      agentsFailed ? 'unavailable'
                   : agentsReady ? agentArray().length + ' connected' : 'checking…'],
     ['Database', dbFailed ? 'warn' : 'ok', dbFailed ? 'unreachable' : 'Ready'],
    ].forEach(([name, tone, value]) => { const chip = document.createElement('span'); chip.className = 'hchip'; chip.innerHTML = `<span class="d ${esc(tone)}"></span>${esc(name)} · ${esc(value)}`; healthRow.append(chip); });
    health.append(healthRow);
    panel.append(viewHeader('Home', 'Your workspace at a glance'), intro, grid, working, usage, recent, health);
  }
  function renderAttention(panel) {
    panel.replaceChildren(); panel.append(viewHeader('Attention', 'Everything waiting for you, in one calm place'));
    const tabs = document.createElement('div'); tabs.className = 'att-tabs';
    [['all','All'],['approval','Approvals'],['question','Questions']].forEach(([key,label]) => { const b = document.createElement('button'); b.type = 'button'; b.className = (state.attentionFilter || 'all') === key ? 'on' : ''; b.textContent = label; b.addEventListener('click', () => { state.attentionFilter = key; showView('attention'); }); tabs.append(b); });
    const tabsWrap = document.createElement('div'); tabsWrap.className = 'att-tabs-wrap'; tabsWrap.append(tabs); panel.append(tabsWrap);
    const items = selectors.attentionItems();
    const filtered = (state.attentionFilter && state.attentionFilter !== 'all') ? items.filter(item => item.kind === state.attentionFilter) : items;
    if (!filtered.length) { const p = document.createElement('p'); p.textContent = 'Nothing needs attention.'; p.className = 'home-empty'; panel.append(p); return; }
    const list = document.createElement('section'); list.className = 'attention-list';
    for (const item of filtered) {
      const article = document.createElement('article'); article.className = 'att-card k-' + item.kind;
      article.innerHTML = `<div class="ac-h"><span class="avatar-fallback">${esc((item.source || '?').slice(0,2).toUpperCase())}</span><span><span class="who">${esc(item.source || 'Workspace')}</span><span class="sub">${esc(item.kind)} · ${esc(timeAgo(item.timestamp) || 'now')}</span></span><span class="waiting"><span class="p"></span>waiting for you</span></div><div class="reason">${esc(item.title)}</div>${item.body ? `<div class="att-detail"><div class="r"><span class="k">Details</span><span class="v">${esc(item.body)}</span></div></div>` : ''}`;
      if (item.actions.length) {
        const row = document.createElement('div'); row.className = 'att-actions';
        for (const d of item.actions) {
          const b = document.createElement('button'); b.type = 'button';
          b.className = (d === 'decline' || d === 'cancel') ? 'abtn danger' : (d === 'acceptForSession' || d === 'dismiss') ? 'abtn soft' : 'abtn ok';
          b.textContent = d === 'accept' ? 'Allow once' : d === 'acceptForSession' ? 'Allow for session' : d === 'openChannel' ? 'Reply in channel' : d === 'dismiss' ? 'Dismiss' : d[0].toUpperCase() + d.slice(1);
          if (d === 'dismiss') {
            // Client-side hide: there's no server dismiss (a question only clears
            // when answered with a selection), so drop it from the inbox and count.
            b.addEventListener('click', () => {
              dismissQuestion(item.id); showView('attention'); renderRail();
              // Permanent hide has no server undo, so give the operator a brief
              // Undo — a mis-click one pixel from "Reply in channel" shouldn't
              // erase an agent's question irreversibly.
              toast('Question dismissed', 6000, { label: 'Undo', onClick: () => { undismissQuestion(item.id); showView('attention'); renderRail(); } });
            });
          } else if (d === 'openChannel') {
            // Jump to the source channel so the operator can reply with text or
            // images in the main composer instead of the picker.
            b.addEventListener('click', () => openChannel(item.channel));
          } else {
            b.disabled = pendingDecisions.has(item.id + ':' + d);
            b.addEventListener('click', () => resolveApproval(item.id, d));
          }
          row.append(b);
        }
        article.append(row);
      }
      list.append(article);
    }
    panel.append(list);
  }
  function timeAgo(iso) { if (!iso) return ''; try { const m = Math.floor((Date.now() - new Date(iso).getTime()) / 60000); if (Number.isNaN(m)) return ''; if (m < 1) return 'just now'; if (m < 60) return m + 'm'; const h = Math.floor(m / 60); if (h < 24) return h + 'h'; return Math.floor(h / 24) + 'd'; } catch { return ''; } }
  function usageTone(pct) { return pct >= 90 ? 'danger' : pct >= 70 ? 'warn' : 'ok'; }
  // Floors each unit rather than rounding so the label is always a safe lower
  // bound (a user who waits the shown time never finds the reset already
  // overdue), and distinguishes an already-past reset ("resets now" — the
  // cached quota read is just stale) from one still pending. Reports mixed
  // units ("2d 9h", "9h 33m") instead of a single floored unit: a coarse "2d"
  // for something 2d9h out reads as an off-by-one to the user, so keep the
  // finer unit alongside. Minutes are shown only under a day, so long windows
  // (the weekly billing period) stay compact.
  function resetLabel(unixSeconds) {
    if (!unixSeconds) return '';
    const ms = unixSeconds * 1000 - Date.now();
    if (ms <= 0) return 'resets now';
    const totalMin = Math.floor(ms / 60000);
    const d = Math.floor(totalMin / 1440);
    const h = Math.floor((totalMin % 1440) / 60);
    const m = totalMin % 60;
    const parts = [];
    if (d) parts.push(`${d}d`);
    if (h) parts.push(`${h}h`);
    if (!d && m) parts.push(`${m}m`);
    if (!parts.length) return 'resets within the minute';
    return `resets in ${parts.join(' ')}`;
  }
  function usageMeter(label, pct, resetsAt, extraHtml) {
    const wrap = document.createElement('div'); wrap.className = 'usage-meter';
    if (pct == null) {
      wrap.innerHTML = `<div class="usage-meter-head"><span>${esc(label)}</span><span class="usage-meter-pct">unknown</span></div>`;
      return wrap;
    }
    const p = Math.max(0, Math.min(100, Math.round(Number(pct) || 0)));
    wrap.innerHTML = `<div class="usage-meter-head"><span>${esc(label)}</span><span class="usage-meter-pct">${esc(String(p))}%${resetsAt ? ' · ' + esc(resetLabel(resetsAt)) : ''}</span></div><div class="usage-meter-track"><div class="usage-meter-fill ${usageTone(p)}" style="width:${p}%"></div></div>${extraHtml || ''}`;
    return wrap;
  }
  // Compact duration: "2d 3h", "4h 12m", "35m", "<1m". Floors each unit so a
  // projected exhaustion reads as a safe lower bound.
  function fmtDur(sec) {
    if (sec == null || !isFinite(sec) || sec < 0) return '';
    const m = Math.floor(sec / 60), d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60), mm = m % 60;
    if (d) return `${d}d${h ? ' ' + h + 'h' : ''}`;
    if (h) return `${h}h${mm ? ' ' + mm + 'm' : ''}`;
    if (mm) return `${mm}m`;
    return '<1m';
  }
  // Quota burn is measured in percentage points per hour (pp/hr), not "percent
  // of the current value". Calling the unit out prevents a 40%→42% move from
  // being misread as a 5% relative increase.
  function trendChip(rate, winKey) {
    if (rate == null) return '';
    const win = { m15: '15m', h1: '1h', h24: '24h' }[winKey];
    const winTag = win ? ` <span class="burn-win">last ${win}</span>` : '';
    if (Math.abs(rate) < 0.05) return `<div class="usage-trend"><span class="burn steady">steady</span>${winTag}</div>`;
    const tone = rate > 0 ? 'up' : 'down';
    return `<div class="usage-trend"><span class="burn ${tone}">${rate > 0 ? '+' : '−'}${Math.abs(rate).toFixed(1)} pp/hr</span>${winTag}</div>`;
  }
  // Default chip: the shortest window with data (the most current trend).
  function trendLine(windows) {
    if (!windows) return '';
    const key = ['m15', 'h1', 'h24'].find(k => windows[k] != null);
    return key == null ? '' : trendChip(windows[key], key);
  }
  function dailyChangeLine(change) {
    if (!change || change.percentage_points == null) return '';
    const pp = Number(change.percentage_points) || 0;
    const hours = Number(change.elapsed_hours) || 0;
    const tone = pp > 0.05 ? 'up' : pp < -0.05 ? 'down' : 'steady';
    const span = hours >= 23.5 ? 'last 24h' : `last ${hours.toFixed(hours < 2 ? 1 : 0)}h`;
    return `<div class="usage-trend"><span class="burn ${tone}">${pp > 0 ? '+' : pp < 0 ? '−' : ''}${Math.abs(pp).toFixed(1)} pp</span> <span class="burn-win">${esc(span)}</span></div>`;
  }
  // Forecast at reset, including the exact linear calculation the usage panel uses.
  // This is deliberately explicit: "40% now + 1.2 pp/hr × 30h = 76%" makes
  // both the selected rate and the assumption behind the projection auditable.
  function projectionLine(proj, stale, quotaLabel) {
    if (!proj) return '';
    quotaLabel = quotaLabel || 'Quota';
    if (proj.exhausted) return `<div class="usage-proj danger">${esc(quotaLabel)} exhausted — waiting for the period to reset.</div>`;
    if (proj.rate_per_hr == null) return '<div class="usage-proj neutral"><strong>Forecast pending</strong><span>Collecting a same-source baseline (at least 1 minute).</span></div>';
    const inLabel = fmtDur(proj.exhaust_at - Date.now() / 1000);
    // '<1m' or '' (already-past, e.g. client clock ahead of server) both read as imminent.
    const when = (inLabel === '<1m' || inLabel === '') ? 'in under a minute' : `in ~${inLabel}`;
    const rate = Math.abs(Number(proj.rate_per_hr) || 0).toFixed(1);
    const windowName = { m15: '15m', h1: '1h', h24: '24h' }[proj.window] || 'available';
    const expected = proj.projected_at_reset == null ? null : Math.max(0, Number(proj.projected_at_reset));
    const tone = expected == null ? 'warn' : usageTone(expected);
    // Use the server's own `current`. Back-deriving it from the projection
    // breaks whenever the projection is clamped at 100%.
    const current = proj.current != null ? Math.max(0, Number(proj.current))
      : (expected != null && proj.hours_to_reset != null
        ? Math.max(0, expected - Number(proj.rate_per_hr) * Number(proj.hours_to_reset)) : null);
    const headline = expected == null ? `${esc(quotaLabel)} forecast`
      : `<strong>${proj.projection_clamped ? 'over ' : ''}${esc(String(Math.round(expected)))}% expected at reset</strong>`;
    const math = current == null ? `${rate} pp/hr · ${windowName} trend`
      : `${Math.round(current)}% now + ${rate} pp/hr × ${fmtDur(Number(proj.hours_to_reset) * 3600)} · ${windowName} trend`;
    let outcome = '';
    if (proj.before_reset === true) outcome = `Reaches 100% ${esc(when)}, before reset.`;
    else if (proj.before_reset === false) outcome = 'Reset arrives before 100% — on track.';
    else if (proj.will_exhaust) outcome = `Reaches 100% ${esc(when)}; reset time unavailable.`;
    const caveat = stale ? '<span>Quota source may be stale.</span>' : '';
    return `<div class="usage-proj ${tone}">${headline}<span>${esc(math)}</span>${outcome ? `<span>${outcome}</span>` : ''}${caveat}</div>`;
  }
  function messageActivity(msgs) {
    if (!msgs) return null;
    const sec = document.createElement('div'); sec.className = 'usage-activity';
    sec.innerHTML = '<div class="ua-head" title="Sent = you (the operator); received = agents and other participants.">Message activity <span>across all channels</span></div>';
    const grid = document.createElement('div'); grid.className = 'ua-grid';
    [['m15', '15 min'], ['h1', '1 hour'], ['h24', '24 hour']].forEach(([k, lbl]) => {
      const w = msgs[k] || { total: 0, sent: 0, received: 0 };
      const cell = document.createElement('div'); cell.className = 'ua-cell';
      cell.innerHTML = `<span class="ua-win">${esc(lbl)}</span><span class="ua-total">${esc(String(w.total))}</span><span class="ua-split">${esc(String(w.sent))} sent · ${esc(String(w.received))} recv</span>`;
      grid.append(cell);
    });
    sec.append(grid);
    return sec;
  }
  // Compact token count: 940, 1.2k, 512k, 1.4M. (~3 significant figures; the
  // 999500 cutoff avoids rounding to a stray "1000k" just below 1M.)
  function fmtNum(n) {
    n = Number(n) || 0;
    if (n < 1000) return String(n);
    if (n < 999500) return (n / 1e3).toFixed(n < 1e4 ? 1 : 0) + 'k';
    return (n / 1e6).toFixed(1) + 'M';
  }
  // Real token consumption across all agents (harvested from stream-json usage).
  function tokenActivity(tokens) {
    if (!tokens) return null;
    const sec = document.createElement('div'); sec.className = 'usage-activity';
    sec.innerHTML = '<div class="ua-head" title="Input = uncached prompt tokens. Cache read = reused context. Cache write = context newly stored for reuse. Output = model-generated tokens. Other = older telemetry or provider-only categories that could not be classified.">Token usage <span>provider + token-type split</span></div>';
    const grid = document.createElement('div'); grid.className = 'ua-grid';
    [['m15', '15 min'], ['h1', '1 hour'], ['h24', '24 hour']].forEach(([k, lbl]) => {
      const w = tokens[k] || { total: 0, providers: {} };
      const providers = w.providers || {};
      const providerBits = [`Claude ${fmtNum(providers.claude?.total)}`, `Codex ${fmtNum(providers.codex?.total)}`];
      if (Number(providers.unknown?.total) > 0) providerBits.push(`legacy/unknown ${fmtNum(providers.unknown.total)}`);
      const providerLine = providerBits.join(' · ');
      const typeBits = [
        ['input', w.input], ['cache read', w.cache_read],
        ['cache write', w.cache_write], ['output', w.output],
      ];
      if (Number(w.other) > 0) typeBits.push(['other', w.other]);
      const typeLine = typeBits.map(([label, value]) => `${label} ${fmtNum(value)}`).join(' · ');
      const cell = document.createElement('div'); cell.className = 'ua-cell';
      cell.innerHTML = `<span class="ua-win">${esc(lbl)}</span><span class="ua-total">${esc(fmtNum(w.total))}</span><span class="ua-split ua-provider-split">${esc(providerLine)}</span><span class="ua-split ua-token-split">${esc(typeLine)}</span>`;
      grid.append(cell);
    });
    sec.append(grid);
    return sec;
  }
  function codexDailyActivity(codex) {
    const buckets = Array.isArray(codex?.daily_usage) ? codex.daily_usage.slice(-7) : [];
    if (!buckets.length) return null;
    const sec = document.createElement('div'); sec.className = 'usage-activity';
    sec.innerHTML = '<div class="ua-head">Codex daily tokens <span>account total · last 7 reported days</span></div>';
    const grid = document.createElement('div'); grid.className = 'codex-days';
    buckets.forEach(bucket => {
      const cell = document.createElement('div'); cell.className = 'codex-day';
      const date = String(bucket.startDate || '');
      cell.innerHTML = `<span>${esc(date.slice(5) || date)}</span><strong>${esc(fmtNum(bucket.tokens))}</strong>`;
      grid.append(cell);
    });
    sec.append(grid);
    return sec;
  }
  // Freshness badge: statusline-state.json only advances while an interactive
  // Claude Code session renders its status bar (headless agents never touch it),
  // so the file's age is a real staleness signal. Flag it so a frozen number
  // reads as stale rather than current. ok <5m, warn <30m, danger beyond.
  function freshnessLine(updatedAt) {
    if (!updatedAt) return null;
    const sec = Math.max(0, Date.now() / 1000 - Number(updatedAt));
    const tone = sec < 300 ? 'ok' : sec < 1800 ? 'warn' : 'danger';
    const ago = sec < 60 ? 'just now' : sec < 3600 ? `${Math.floor(sec / 60)}m ago`
      : sec < 86400 ? `${Math.floor(sec / 3600)}h ago` : `${Math.floor(sec / 86400)}d ago`;
    const row = document.createElement('div'); row.className = 'usage-asof ' + tone;
    row.title = 'Claude Code only refreshes these figures while an interactive session renders its status bar — this is when it last did.';
    // "Quota updated" (not just "Updated") so it's clear the age describes the
    // Claude quota figures, not the whole panel — message activity below is live.
    row.innerHTML = `<span class="asof-dot"></span>Quota updated ${esc(ago)}${tone === 'ok' ? '' : ' · may be stale'}`;
    return row;
  }
  function usageMeters(usage) {
    const wrap = document.createElement('div'); wrap.className = 'usage-meters';
    const claude = usage?.claude;
    const burn = usage?.burn;
    if (claude?.available) {
      const fresh = freshnessLine(claude.updated_at);
      if (fresh) wrap.append(fresh);
      const stale = claude.updated_at ? (Date.now() / 1000 - Number(claude.updated_at)) >= 1800 : false;
      const fhProjection = burn?.projections?.five_hour;
      wrap.append(usageMeter('Claude Code · 5 hour', claude.five_hour?.used_percentage, claude.five_hour?.resets_at,
        dailyChangeLine(burn?.daily_change?.five_hour) + trendLine(burn?.five_hour) + projectionLine(fhProjection, stale, '5-hour quota')));
      // When the weekly meter is actively forecasting exhaustion, show the chip
      // for the SAME window the projection used, so the trend and the forecast
      // can't contradict each other. Otherwise show the current (shortest) trend.
      const proj = burn?.projections?.seven_day || burn?.projection;
      const weeklyTrend = (proj && proj.will_exhaust && proj.rate_per_hr != null)
        ? trendChip(proj.rate_per_hr, proj.window)
        : trendLine(burn?.seven_day);
      wrap.append(usageMeter('Claude Code · weekly', claude.seven_day?.used_percentage, claude.seven_day?.resets_at,
        dailyChangeLine(burn?.daily_change?.seven_day) + weeklyTrend + projectionLine(proj, stale, 'Weekly quota')));
    } else {
      const p = document.createElement('p'); p.className = 'home-empty'; p.textContent = 'Claude Code usage data not available.'; wrap.append(p);
    }
    if (usage?.codex?.available) {
      const codexHead = document.createElement('div'); codexHead.className = 'usage-provider-head';
      codexHead.innerHTML = '<strong>Codex</strong><span>ChatGPT account limits</span>';
      wrap.append(codexHead);
      (usage.codex.quotas || []).forEach(q => {
        const duration = q.window_duration_mins ? (q.window_duration_mins < 60
          ? `${Math.round(q.window_duration_mins)} min`
          : q.window_duration_mins < 1440 ? `${Math.round(q.window_duration_mins / 60)} hour`
          : `${Math.round(q.window_duration_mins / 1440)} day`) : q.kind;
        const label = `Codex · ${q.label}${duration ? ' · ' + duration : ''}`;
        const chosen = q.projection?.will_exhaust && q.projection?.rate_per_hr != null
          ? trendChip(q.projection.rate_per_hr, q.projection.window) : trendLine(q.burn);
        wrap.append(usageMeter(label, q.used_percentage, q.resets_at,
          dailyChangeLine(q.daily_change) + chosen + projectionLine(q.projection, false, q.label)));
      });
      if (!(usage.codex.quotas || []).length) {
        const p = document.createElement('p'); p.className = 'home-empty'; p.textContent = 'Codex account connected; no quota windows were returned.'; wrap.append(p);
      }
      const daily = codexDailyActivity(usage.codex);
      if (daily) wrap.append(daily);
    }
    const tokens = tokenActivity(usage?.tokens);
    if (tokens) wrap.append(tokens);
    const activity = messageActivity(usage?.messages);
    if (activity) wrap.append(activity);
    if (!usage?.codex?.available) {
      const p = document.createElement('p'); p.className = 'home-empty';
      p.textContent = usage?.codex?.reason === 'no_managed_agent'
        ? 'Codex account usage appears after a managed Codex agent is added.'
        : 'Codex account usage is temporarily unavailable.';
      wrap.append(p);
    }
    return wrap;
  }
  function openTaskModal(prefill = {}) {
    // Tasks live in a channel (server creates them via a "$task …" post — same
    // flow as trio_send(task=True)), so the operator picks the target channel.
    const channels = (state.channels || []).filter(c => !c.archived);
    if (!channels.length) { toast('Create a channel first — tasks live in a channel.'); return; }
    // Keep the operator's pick across a failed retry; else default to the
    // current conversation's channel, else the first available channel.
    const preferred = prefill.channel && channels.some(c => c.code === prefill.channel) ? prefill.channel
      : (state.channel && channels.some(c => c.code === state.channel) ? state.channel : channels[0].code);
    const options = channels.map(c => `<option value="${esc(c.code)}"${c.code === preferred ? ' selected' : ''}>#${esc(c.code)}</option>`).join('');
    const body = `<label>Task<input id="new-task-title" name="title" required autocomplete="off" maxlength="500" placeholder="What needs doing?" value="${esc(prefill.title || '')}"></label><label>Channel<select name="channel">${options}</select></label>`;
    modal('New task', body, async node => {
      const f = new FormData(node.querySelector('form'));
      const title = String(f.get('title') || '').trim();
      const channel = String(f.get('channel') || '').trim();
      // Reopen pre-filled so the operator never loses what they typed.
      if (!title || !channel) { openTaskModal({ title, channel }); toast('A task needs a description and a channel.'); return; }
      try {
        // Explicit ?channel= (api.url keeps it — CHANNEL_RE guard) so the task
        // is created in the picked channel, not the current view (which has none).
        await api.post('/api/send?channel=' + encodeURIComponent(channel), { content: '$task ' + title }, false);
        toast('Task created in #' + channel);
        try { const data = await api.get('/api/tasks', false); state.tasks = data.tasks || []; Trio.store?.set?.('workspace.tasks', state.tasks); } catch (_) {}
        state.taskFilter = 'open';
        showView('tasks');
      } catch (error) {
        openTaskModal({ title, channel });
        toast(error.message || 'Could not create task');
      }
    });
    // Focus the description, not the modal's × button (first focusable by default).
    document.getElementById('new-task-title')?.focus();
  }
  function renderTasks(panel) {
    panel.replaceChildren();
    panel.append(viewHeader('Tasks', 'Claimable work across every channel',
      { label: 'New task', icon: navIcon('plus'), onClick: openTaskModal }));
    const filters = ['open', 'claimed', 'blocked', 'done', 'all'];
    const filter = filters.includes(state.taskFilter) ? state.taskFilter : 'open';
    const filterBar = document.createElement('div'); filterBar.className = 'att-tabs';
    for (const f of filters) {
      const b = document.createElement('button'); b.type = 'button'; b.textContent = f[0].toUpperCase() + f.slice(1);
      b.className = f === filter ? 'on' : '';
      b.addEventListener('click', () => { state.taskFilter = f; showView('tasks'); });
      filterBar.append(b);
    }
    // Wrap in a block .att-tabs-wrap (like Messages/Attention): .workspace-view>*
    // centers each child at max-width:1040px via margin:auto, but an inline-flex
    // .att-tabs ignores auto margins and sticks to the far left. The block
    // wrapper takes the centering so the filter lines up with the page heading.
    const filterWrap = document.createElement('div'); filterWrap.className = 'att-tabs-wrap'; filterWrap.append(filterBar);
    panel.append(filterWrap);
    const counts = { open: 0, claimed: 0, blocked: 0, done: 0, all: 0 };
    const all = selectors.taskItems();
    for (const t of all) { counts[t.status] = (counts[t.status] || 0) + 1; counts.all++; }
    const list = document.createElement('div'); list.className = 'task-list';
    const rows = filter === 'all' ? all : all.filter(t => t.status === filter);
    if (!rows.length) { const p = document.createElement('p'); p.className = 'home-empty'; p.textContent = 'No ' + (filter === 'all' ? '' : filter + ' ') + 'tasks.'; list.append(p); }
    for (const t of rows) {
      const row = document.createElement('article'); row.className = 'task-row';
      const stateClass = ['open','claimed','blocked','done'].includes(t.status) ? t.status : 'cancelled';
      row.innerHTML = `<span class="tnum">#${esc(t.id)}</span><span class="tmain"><span class="tdesc">${esc(t.title)}</span><span class="tmeta"><span class="tstate ${stateClass}"><span class="d"></span>${esc(t.status)}</span>${t.channel ? `<span>#${esc(t.channel)}</span>` : ''}${t.owner ? `<span>· ${esc(t.owner)}</span>` : ''}${t.blockers.length ? `<span class="dep-chip">depends on ${esc(t.blockers.join(', '))}</span>` : ''}</span></span>`;
      list.append(row);
    }
    panel.append(list);
    const count = document.createElement('p'); count.className = 'task-count'; count.textContent = `open ${counts.open} · claimed ${counts.claimed} · blocked ${counts.blocked} · done ${counts.done}`;
    panel.append(count);
  }
  function renderMessages(panel) {
    panel.replaceChildren(); panel.append(viewHeader('Messages', 'Mentions and direct messages, with read/unread state'));
    const tabs = document.createElement('div'); tabs.className = 'att-tabs';
    const tabKeys = [['all','All'],['mentions','Mentions'],['dms','DMs']];
    const filter = tabKeys.map(t => t[0]).includes(state.messagesTab) ? state.messagesTab : 'all';
    for (const [key, label] of tabKeys) {
      const b = document.createElement('button'); b.type = 'button'; b.textContent = label;
      const count = key === 'all' ? selectors.unreadDms() + selectors.unreadMentions() : key === 'mentions' ? selectors.unreadMentions() : selectors.unreadDms();
      if (count) { const badge = document.createElement('span'); badge.className = 'mini-badge'; badge.textContent = String(count); b.append(badge); }
      b.className = filter === key ? 'on' : '';
      b.addEventListener('click', () => { state.messagesTab = key; showView('messages'); });
      tabs.append(b);
    }
    const tabsWrap = document.createElement('div'); tabsWrap.className = 'att-tabs-wrap'; tabsWrap.append(tabs); panel.append(tabsWrap);
    const list = document.createElement('section'); list.className = 'attention-list';

    function mentionCard(m) {
      const article = document.createElement('article'); article.className = 'att-card k-mention' + (m.read ? '' : ' unread');
      const source = m.member_name || m.member_id || 'Unknown';
      const head = `<div class="ac-h">${avatarFor({ id: m.member_id, name: m.member_name })}<span><span class="who">${esc(source)}</span><span class="sub">#${esc(m.channel)} · ${esc(timeAgo(m.created_at) || 'now')}</span></span>${m.read ? '' : '<span class="waiting"><span class="p"></span>unread</span>'}</div>`;
      const body = `<div class="reason">${Trio.markdown.renderMarkdown(m.content || '')}</div>`;
      const actions = document.createElement('div'); actions.className = 'att-actions';
      if (!m.read) {
        const readBtn = document.createElement('button'); readBtn.type = 'button'; readBtn.className = 'abtn ok'; readBtn.textContent = 'Mark read';
        readBtn.addEventListener('click', (e) => { e.stopPropagation(); markMessagesRead([m.id]).then(() => { m.read = true; if (state.messagesTab === 'all' || state.messagesTab === 'mentions') renderMessages(panel); else showView('messages'); }); });
        actions.append(readBtn);
      }
      article.innerHTML = head + body;
      article.append(actions);
      article.addEventListener('click', () => { markMessagesRead([m.id]).then(() => { m.read = true; openChannel(m.channel); setTimeout(() => { const card = document.querySelector(`[data-message-id="${m.id}"]`); if (card) { card.scrollIntoView({ behavior: 'smooth', block: 'center' }); card.focus(); } }, 300); }); });
      return article;
    }

    function dmCard(dm) {
      const article = document.createElement('article'); article.className = 'att-card k-mention' + (dm.unread ? ' unread' : '');
      const label = dm.name || dm.key || 'Conversation';
      const head = `<div class="ac-h">${avatarFor(label)}<span><span class="who">${esc(label)}</span><span class="sub">Direct message · ${esc(timeAgo(dm.last_at) || 'now')}</span></span>${dm.unread ? `<span class="waiting"><span class="p"></span>${Number(dm.unread) || ''} unread</span>` : ''}</div>`;
      // The DM preview is a truncated (~120-char) content slice, so render it as
      // escaped plain text — markdown on a snippet would leave broken/partial
      // syntax, and a leading list/heading/fence would break the one-line layout
      // (LOTC/Frodo). Full markdown formatting lives in the mention cards + thread.
      const body = `<div class="reason msg-line"><span class="msg-from">${esc(dm.from || 'Someone')}:</span> ${esc(dm.preview || '')}</div>`;
      article.innerHTML = head + body;
      article.addEventListener('click', () => { openDm(dm); });
      return article;
    }

    const showMentions = filter === 'all' || filter === 'mentions';
    const showDms = filter === 'all' || filter === 'dms';
    let items = [];
    if (showMentions) { for (const m of listOf(state.mentions)) items.push({ ...m, kind: 'mention' }); }
    // Same stale rule as the sidebar, so the two surfaces agree about what is
    // "old". Unread and the open thread stay, per isStaleThread.
    let staleDmCount = 0;
    if (showDms) {
      const openId = state.dmKey || '';
      for (const d of (state.dms?.your_dms || [])) {
        if (d.archived) continue;
        if (!state.staleOpen?.messages && isStaleThread(d, openId)) { staleDmCount++; continue; }
        items.push({ ...d, kind: 'dm' });
      }
    }
    items.sort((a, b) => new Date(b.created_at || b.last_at || 0) - new Date(a.created_at || a.last_at || 0));

    if (!items.length && !staleDmCount) {
      const p = document.createElement('p'); p.className = 'home-empty'; p.textContent = 'No messages.';
      list.append(p);
    } else {
      for (const item of items) {
        list.append(item.kind === 'mention' ? mentionCard(item) : dmCard(item));
      }
    }
    // Never let the filter make the page look empty with no explanation: if
    // everything here is old, the reason has to be on screen and reversible.
    if (staleDmCount) {
      const more = document.createElement('button');
      more.type = 'button'; more.className = 'nav-stale-toggle';
      more.textContent = 'Show ' + staleDmCount + ' older conversation' + (staleDmCount === 1 ? '' : 's');
      more.addEventListener('click', () => {
        state.staleOpen = state.staleOpen || {}; state.staleOpen.messages = true; renderMessages(panel);
      });
      list.append(more);
    }
    if (filter === 'mentions' && listOf(state.mentions).some(m => !m.read)) {
      const footer = document.createElement('div'); footer.className = 'att-actions';
      const allBtn = document.createElement('button'); allBtn.type = 'button'; allBtn.className = 'abtn ok'; allBtn.textContent = 'Mark all mentions read';
      allBtn.addEventListener('click', async () => {
        const ids = listOf(state.mentions).filter(m => !m.read).map(m => m.id);
        await markMessagesRead(ids);
        listOf(state.mentions).forEach(m => m.read = true);
        renderMessages(panel);
        renderRail();
      });
      footer.append(allBtn); panel.append(footer);
    }
    panel.append(list);
  }
  function showView(view) {
    // A route lookup for a DM may still be awaiting metadata. Leaving for a
    // workspace page invalidates that navigation before clearing its visible
    // identity; otherwise the late promise can reopen the private thread the
    // operator explicitly left and move browser history back into it.
    navGen++;
    Trio.loader?.cancelAll?.('dm:');
    state.view = view;
    state.channel = '';
    state.dmKey = '';
    state.dmName = '';
    state.dmRouteResolved = true;
    state.readOnly = false;
    state.members = new Map();
    Trio.stopEvents?.();
    closeDetails();
    $('app')?.classList.remove('channel-details-open');
    const detailsBtn = $('details-btn');
    const moreBtn = $('channel-more-btn');
    const searchBtn = $('search-btn');
    const conn = $('h-conn');
    if (detailsBtn) detailsBtn.classList.add('hidden');
    if (moreBtn) moreBtn.classList.add('hidden');
    if (searchBtn) searchBtn.classList.add('hidden');
    if (conn) conn.classList.add('hidden');
    const shell = document.querySelector('.conversation-shell');
    shell?.classList.add('workspace-page');
    updateTopbar(view === 'home' ? 'nth' : view[0].toUpperCase() + view.slice(1), view === 'home' ? 'Home' : `trio view · ${view}`);
    document.querySelectorAll('[data-trio-view]').forEach(n => n.hidden = true);
    let panel = $(`trio-${view}-view`);
    if (!panel) { panel = document.createElement('section'); panel.id = `trio-${view}-view`; panel.dataset.trioView = view; panel.className = 'workspace-view'; document.querySelector('.conversation-shell')?.prepend(panel); }
    panel.hidden = false;
    if (view === 'home') { renderHome(panel); }
    else if (view === 'tasks') { renderTasks(panel); }
    else if (view === 'attention') { renderAttention(panel); }
    else if (view === 'messages') { renderMessages(panel); }
    else if (view === 'roster') { Trio.agents?.renderPage?.(panel); }
    else if (view === 'prefs') { Trio.preferences?.renderPage?.(panel); }
    else if (view === 'archive') { renderArchive(panel); }
    // Data page is owned by a sibling module (js/45-data.js → Trio.data). The
    // hook keeps this shell decoupled from the storage/prune implementation.
    else if (view === 'data') { (Trio.data?.renderPage || renderDataStub)(panel); }
    else panel.innerHTML = `<h2>Home</h2><p>${(state.channels || []).length} active channels · ${(state.dms?.your_dms || []).length} direct conversations</p>`;
    renderFacePile();
    renderRail();
  }
  function createChannel(prefill = {}) {
    // The create modal is a method="dialog" form: it closes the instant Save
    // is clicked, and this submit handler runs afterward. A failed create
    // would therefore vanish silently ("modal closed, no channel"). To make
    // failure visible and retryable we surface the server's actual error and
    // reopen the form pre-filled with what the operator typed. The code is
    // trimmed/lowercased before submit so a stray space or capital just works
    // instead of the browser's cryptic native-pattern bubble; anything the
    // server still rejects comes back as a clear toast (and a reopened form),
    // so we deliberately don't set an HTML `pattern` here.
    const body = `<label>Channel code<input name="code" required value="${esc(prefill.code || '')}"></label><label>Topic<input name="topic" value="${esc(prefill.topic || '')}"></label>`;
    modal('Create channel', body, async node => {
      const f = new FormData(node.querySelector('form'));
      const code = String(f.get('code') || '').trim().toLowerCase();
      const topic = String(f.get('topic') || '').trim();
      try {
        await api.post('/api/channels', { code, topic });
        openChannel(code);
      } catch (error) {
        // Reopen the modal FIRST, then toast. Both live in the top layer and
        // paint in insertion order, so the toast (a popover) must be added
        // AFTER the reopened dialog to stack above its blurred backdrop —
        // otherwise the failure reason is hidden behind the blur.
        createChannel({ code, topic });
        toast(error.message || 'Could not create channel');
      }
    });
  }
  function dmTargets() {
    const targets = state.dms?.targets || [];
    if (targets.length) return targets.slice().sort((a, b) => String(a.name || a.id).localeCompare(String(b.name || b.id)));
    return (Trio.store?.get('agents.list') || state.agents || []).map(agent => ({id: agent.id, name: agent.name || agent.id, dm_channel: agent.dm_channel})).sort((a, b) => String(a.name).localeCompare(String(b.name)));
  }
  function openDmDialog() {
    const targets = dmTargets();
    const options = targets.map(target => `<option value="${esc(target.id)}">${esc(target.name || target.id)}</option>`).join('');
    modal('Start a direct message', `<p>Choose an agent to open a private conversation with.</p><label for="dm-agent">Agent<select id="dm-agent" name="agent" required ${targets.length ? '' : 'disabled'}><option value="">Select an agent…</option>${options}</select></label>${targets.length ? '' : '<p class="home-empty">No agents are available yet.</p>'}`, node => {
      const id = new FormData(node.querySelector('form')).get('agent');
      const target = targets.find(item => item.id === id);
      if (!target) return;
      const existing = (state.dms?.your_dms || []).find(dm => dm.key === id);
      openDm(existing || {key: id, member_ids: [id], name: target.name || id, channel: target.dm_channel || state.channel});
    });
    document.getElementById('dm-agent')?.focus();
  }
  function viewArchiveChannel(code) { loadConversation(code, 'trio#' + code, 'Archived channel — read only', true, false); }
  function viewArchiveDm(dm) { openDm(dm, true); }
  function buildArchiveList(container, items, kind, onRestore) {
    container.replaceChildren();
    if (!items.length) { const p = document.createElement('p'); p.className = 'home-empty'; p.textContent = 'Nothing archived.'; container.append(p); return; }
    const q = (state.archiveSearch || '').toLowerCase();
    const filtered = q ? items.filter(x => (x.code || x.name || x.key || '').toLowerCase().includes(q)) : items;
    if (!filtered.length) { const p = document.createElement('p'); p.className = 'home-empty'; p.textContent = 'No matches.'; container.append(p); return; }
    for (const x of filtered) {
      const key = x.code || x.key; const label = x.code || x.name || x.key;
      const li = document.createElement('li'); li.className = 'archive-row';
      // A DM archived because its agent was archived has no DM-archive record
      // to delete, so "Restore" here would silently do nothing — say what
      // actually brings it back instead of offering a dead button.
      const restore = x.agent_archived
        ? '<span class="archive-note">Unarchive the agent to restore</span>'
        : `<button data-kind="${esc(kind)}" data-action="restore" data-key="${esc(key)}">Restore</button>`;
      li.innerHTML = `<span class="archive-label">${esc(label)}</span><span class="archive-actions"><button data-kind="${esc(kind)}" data-action="view" data-key="${esc(key)}">View</button>${restore}</span>`;
      li.querySelectorAll('button').forEach(b => b.addEventListener('click', () => {
        if (b.dataset.action === 'view') b.dataset.kind === 'channel' ? viewArchiveChannel(b.dataset.key) : viewArchiveDm(x);
        else Promise.resolve(archive(b.dataset.kind, b.dataset.key, false)).then(() => onRestore?.(x)).catch(() => {});
      }));
      container.append(li);
    }
  }
  async function showArchives() {
    try {
      const [channels, dms] = await Promise.all([api.get('/api/channels?archived=1'), api.get('/api/dms?archived=1')]);
      const archivedChannels = channels.channels || []; const archivedDms = dms.your_dms || [];
      let panel = $('trio-archives'); if (!panel) { panel = document.createElement('dialog'); panel.id = 'trio-archives'; panel.className = 'archive-modal'; document.body.append(panel); }
      Trio.ui.configureDialog(panel);
      panel.innerHTML = '<form method="dialog"><button type="submit" formnovalidate class="modal-close" value="cancel">×</button><h2>Archives</h2><input class="archive-search" placeholder="Filter archived…" aria-label="Filter archived"><section><h3>Channels</h3><ul class="archive-channel-list"></ul></section><section><h3>Direct messages</h3><ul class="archive-dm-list"></ul></section></form>';
      const cList = panel.querySelector('.archive-channel-list'); const dList = panel.querySelector('.archive-dm-list');
      buildArchiveList(cList, archivedChannels, 'channel'); buildArchiveList(dList, archivedDms, 'dm');
      const input = panel.querySelector('.archive-search');
      input.addEventListener('input', () => { state.archiveSearch = input.value; buildArchiveList(cList, archivedChannels, 'channel'); buildArchiveList(dList, archivedDms, 'dm'); });
      panel.showModal();
    } catch (error) { toast(error.message || 'Could not load archives'); }
  }
  // Archive as a full workspace page (reached from the account menu), reusing the
  // same list builder + endpoints as the legacy dialog.
  async function renderArchive(panel) {
    panel.innerHTML = `<div class="page-head"><h2>Archive</h2><p class="page-sub">Browse and restore archived channels and direct messages.</p></div>`
      + `<input class="archive-search page-search" id="archive-page-search" placeholder="Filter archived…" aria-label="Filter archived">`
      + `<section class="archive-section"><h3>Channels</h3><ul class="archive-channel-list"></ul></section>`
      + `<section class="archive-section"><h3>Direct messages</h3><ul class="archive-dm-list"></ul></section>`;
    const cList = panel.querySelector('.archive-channel-list');
    const dList = panel.querySelector('.archive-dm-list');
    cList.innerHTML = '<li class="home-empty">Loading…</li>';
    try {
      const [channels, dms] = await Promise.all([api.get('/api/channels?archived=1'), api.get('/api/dms?archived=1')]);
      const archivedChannels = channels.channels || []; const archivedDms = dms.your_dms || [];
      // On restore, drop the row from its list and repaint so the page reflects
      // reality immediately (a persistent page, unlike the old transient dialog).
      const onRestore = item => {
        const key = item.code || item.key;
        for (const arr of [archivedChannels, archivedDms]) {
          const i = arr.findIndex(x => (x.code || x.key) === key);
          if (i >= 0) arr.splice(i, 1);
        }
        paint();
        toast('Restored');
      };
      const paint = () => { buildArchiveList(cList, archivedChannels, 'channel', onRestore); buildArchiveList(dList, archivedDms, 'dm', onRestore); };
      paint();
      const input = panel.querySelector('#archive-page-search');
      input?.addEventListener('input', () => { state.archiveSearch = input.value; paint(); });
    } catch (error) { toast(error.message || 'Could not load archives'); cList.innerHTML = ''; }
  }
  // Shown only if someone hits /data directly while the Data module (Trio.data,
  // js/46-data.js) isn't loaded — the account-menu item is hidden in that case,
  // so this is a direct-URL fallback. Honest, not a fake "loading…".
  function renderDataStub(panel) {
    panel.innerHTML = `<div class="page-head"><h2>Data</h2><p class="page-sub">Storage management isn't available in this build yet.</p></div>`;
  }
  async function archiveCurrent() {
    const target = state.dmKey ? 'this DM' : (state.channel ? 'this channel' : '');
    if (!target) { Trio.ui.toast('No conversation to archive'); return; }
    // Which direction this goes is decided by state.readOnly, and the prompt
    // has to be built from the SAME value. It used to be hardcoded "Archive
    // …?" while the direction was computed inside the callback, so opening an
    // archived conversation and clicking the menu item — correctly labelled
    // "Restore channel" — asked "Archive this channel?" instead. Anyone who
    // reads their confirmations cancels; anyone who does not gets the right
    // outcome by luck.
    const nextArchived = !state.readOnly;
    const verb = nextArchived ? 'Archive' : 'Restore';
    Trio.ui.confirmAction(`${verb} ${target}?`, () => {
      if (state.dmKey) {
        const title = 'DM ' + state.dmName;
        archive('dm', state.dmKey, nextArchived).then(() =>
          loadConversation(state.channel, title, nextArchived ? 'Archived private conversation' : 'Private conversation', nextArchived, true));
      } else if (state.channel) {
        const title = 'trio#' + state.channel;
        archive('channel', state.channel, nextArchived).then(() =>
          loadConversation(state.channel, title, nextArchived ? 'Archived channel — read only' : 'Live agent workspace', nextArchived, false));
      }
    });
  }
  const menuIcon = {
    details: '<path d="M13 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-7"/><path d="M9 9h5M9 13h6M9 17h4"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.2-3.2"/>',
    mute: '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="m9 21h6"/>',
    archive: '<path d="M4 7h16v13H4zM3 4h18v3H3zM9 11h6"/>',
  };
  function menuSvg(name) { return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${menuIcon[name] || ''}</svg>`; }
  function closeChannelMenu() {
    const menu = $('channel-menu'); const button = $('channel-more-btn');
    if (!menu) return;
    menu.hidden = true; menu.classList.remove('open');
    button?.classList.remove('menu-active'); button?.setAttribute('aria-expanded', 'false');
  }
  function openChannelMenu() {
    const menu = $('channel-menu'); const button = $('channel-more-btn');
    if (!menu || !button) return;
    if (!menu.hidden) { closeChannelMenu(); return; }
    const isChannel = !!state.channel && !state.dmKey;
    const archived = !!state.readOnly;
    // LOTC/Frodo: this used to be a stub — clicking it just toasted "muted"
    // and did nothing, which cross-channel chimes turned from a cosmetic gap
    // into a real "the only escape hatch is fake" problem. The key here must
    // match what Trio.notifications.conversationKeyFor() resolves for a live
    // message in this same conversation (channel code, or 'dm:'+dmKey).
    const muteKey = state.dmKey ? 'dm:' + state.dmKey : state.channel;
    const muted = Trio.notifications?.isMuted?.(muteKey);
    const rows = [
      `<button type="button" role="menuitem" data-menu-action="details">${menuSvg('details')}<span>${isChannel ? 'Channel details' : 'Conversation details'}</span></button>`,
      `<button type="button" role="menuitem" data-menu-action="search">${menuSvg('search')}<span>${isChannel ? 'Search this channel' : 'Search this conversation'}</span></button>`,
      `<button type="button" role="menuitem" data-menu-action="mute">${menuSvg('mute')}<span>${muted ? 'Unmute notifications' : 'Mute notifications'}</span></button>`,
      '<div class="menu-sep" role="separator"></div>',
      isChannel
        ? `<button type="button" role="menuitem" class="danger" data-menu-action="archive">${menuSvg('archive')}<span>${archived ? 'Restore channel' : 'Archive channel'}</span></button>`
        : `<button type="button" role="menuitem" data-menu-action="archive">${menuSvg('archive')}<span>${archived ? 'Restore conversation' : 'Archive conversation'}</span></button>`,
    ];
    menu.innerHTML = rows.join(''); menu.hidden = false; menu.classList.add('open');
    button.classList.add('menu-active'); button.setAttribute('aria-expanded', 'true');
    const rect = button.getBoundingClientRect();
    const width = menu.offsetWidth || 218;
    const top = Math.min(window.innerHeight - 12 - (menu.offsetHeight || 190), rect.bottom + 8);
    menu.style.left = Math.max(12, rect.right - width) + 'px'; menu.style.top = Math.max(12, top) + 'px';
    menu.querySelectorAll('[data-menu-action]').forEach(item => item.addEventListener('click', () => {
      const action = item.dataset.menuAction;
      if (action === 'details') showDetails();
      if (action === 'search') openSearch();
      if (action === 'mute') {
        closeChannelMenu();
        const nowMuted = Trio.notifications?.toggleMute?.(muteKey);
        toast(nowMuted ? 'Notifications muted for this conversation' : 'Notifications unmuted for this conversation');
      }
      if (action === 'archive') { closeChannelMenu(); archiveCurrent(); }
    }));
    menu.querySelector('button')?.focus();
  }
  // ── Account menu (inline disclosure in the sidebar footer, opened from the
  //    name/avatar). Expanding #account-items reflows the bar upward within the
  //    pane — no floating sheet, no backdrop. ─────────────────────────────────
  function closeAccountMenu() {
    const foot = $('account'); const items = $('account-items');
    // Restore focus to the trigger only if focus currently lives inside the
    // revealed region (Esc / outside-click / item-pick). Gate on the region
    // itself, NOT the deferred `.open` class — a close in the pre-rAF window
    // (e.g. Esc right after opening) must still restore focus instead of
    // orphaning it on the item we're about to make inert (Uruk-Hai/Sauron).
    const restoreFocus = !!(items && items.contains(document.activeElement));
    if (foot) foot.classList.remove('open');
    if (items) { items.setAttribute('aria-hidden', 'true'); items.setAttribute('inert', ''); }
    const trigger = $('account-trigger');
    trigger?.setAttribute('aria-expanded', 'false');
    if (restoreFocus) trigger?.focus();
  }
  function openAccountMenu() {
    const foot = $('account'); const items = $('account-items');
    if (!foot || !items) return;
    // In the collapsed 56px rail the inline menu can't render — expand the
    // sidebar instead of lighting up a dead toggle (Frodo); the next click opens.
    if (document.getElementById('app')?.classList.contains('sidebar-collapsed')) {
      Trio.sidebar?.toggle?.();
      return;
    }
    const trigger = $('account-trigger');
    // Toggle on the SYNCHRONOUS aria-expanded, not the deferred `.open` class,
    // so a re-click inside the double-rAF window closes rather than re-opens
    // (Sauron). This keeps the open-check and the rAF guard below in agreement.
    if (trigger?.getAttribute('aria-expanded') === 'true') { closeAccountMenu(); return; }
    const list = [
      { view: 'prefs', icon: 'settings', label: 'Preferences' },
      { view: 'archive', icon: 'archive', label: 'Archive' },
      // Data page ships in a sibling module (Trio.data). Only offer it once that
      // module is loaded, so the menu never points at a not-yet-available page.
      ...(Trio.data?.renderPage ? [{ view: 'data', icon: 'database', label: 'Data' }] : []),
    ];
    // Reuse the rail's .nav-item structure (plain icon + single-line label) so
    // these read identically to the top nav items; `account-item` is just a
    // behavioral hook for the click wiring / tests.
    items.innerHTML = `<div class="account-items-inner"><div class="account-menu-list">`
      + list.map(it => `<button type="button" class="nav-item account-item" data-view="${esc(it.view)}"><span class="nav-hash">${navIcon(it.icon)}</span><span class="nav-label">${esc(it.label)}</span></button>`).join('')
      + `</div></div>`;
    items.removeAttribute('inert');
    items.setAttribute('aria-hidden', 'false');
    trigger?.setAttribute('aria-expanded', 'true');
    // Double rAF so the browser paints the collapsed (0fr) state once before
    // .open flips it to 1fr — a single rAF can slip and pop instead of expand.
    // The aria-expanded guard drops both the class-add AND the focus move if a
    // fast close already fired; focusing here (after the region has height)
    // avoids scrolling the nav list to reveal a 0-height clipped item (Frodo).
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (trigger?.getAttribute('aria-expanded') === 'true') {
        foot.classList.add('open');
        items.querySelector('button')?.focus();
      }
    }));
    items.querySelectorAll('.account-item').forEach(btn => btn.addEventListener('click', () => {
      const view = btn.dataset.view;
      closeAccountMenu();
      navigateView(view);
    }));
  }
  let menuClick = null, menuKeydown = null, menuButtonClick = null, accountTriggerClick = null;
  let drawerResizeStart = null, drawerResizeMove = null, drawerResizeEnd = null;
  let unroute = null;
  let wsl = null;
  let preferencesChanged = null;
  function onWorkspaceUpdate() { if (['home','attention','messages','tasks'].includes(state.view)) showView(state.view); }
  // Saving "Hide old threads" already broadcasts this event. The rail reads
  // that preference when it groups channels and DMs, so repaint immediately
  // instead of leaving the old grouping visible until the 15-second refresh.
  function onPreferencesChanged() { renderRail(); }
  function onRoute(route) {
    if (!route) return;
    if (route.name === 'channel') {
      // One full load, including when the code has not changed. There used to
      // be a partial same-code branch here that hand-rolled the DM teardown
      // (the "Back from a DM whose transport IS this channel" case). It cleared
      // the DM's identity but left its MESSAGE MAP in place and never restarted
      // channel events, so that transition landed on a channel showing the DM's
      // history over a stream still scoped to the DM. loadConversation already
      // does all of it correctly; duplicating a subset of its work was the bug,
      // not the duplication of the call.
      loadConversation(route.params.code, '#' + route.params.code,
                       channelSubtitle(!!route.params.archived), !!route.params.archived, false);
    }
    else if (route.name === 'dm' || route.name === 'audit') {
      const audit = route.name === 'audit';
      const archived = route.name === 'dm' && !!route.params.archived;
      const currentArchived = !!state.readOnly && !state.dmAudit;
      if (state.dmKey !== route.params.key || state.dmAudit !== audit || currentArchived !== archived) {
        beginDmRoute(route.params.key, audit, archived);
        openDmByKey(route.params.key, audit, true, archived);
      } else routeFeedClaimed = true;
    }
    else if (route.name === 'home') showView('home');
    else if (route.name === 'attention') showView('attention');
    else if (route.name === 'messages') showView('messages');
    else if (route.name === 'tasks') showView('tasks');
    else if (route.name === 'roster') showView('roster');
    else if (route.name === 'prefs') showView('prefs');
    else if (route.name === 'archive') showView('archive');
    else if (route.name === 'data') showView('data');
  }
  async function refresh() {
    if (state.workspaceLoading) return;
    state.workspaceLoading = true; state.workspaceError = '';
    renderRail();
    const query = state.channel ? '?channel=' + encodeURIComponent(state.channel) : '';
    const requests = [
      api.get('/api/channels').then(data => { state.channels = data.channels || []; Trio.store.set('workspace.channels', state.channels); renderRail(); markLoaded('channels'); }),
      api.get('/api/dms').then(data => { state.dms = data; Trio.store.set('workspace.dms', state.dms); renderRail(); markLoaded('dms'); }),
      api.get('/api/meta' + query).then(data => { state.meta = {...state.meta, ...data}; Trio.store.set('workspace.meta', state.meta); renderRail(); markLoaded('meta'); }),
      api.get('/api/tasks' + query).then(data => { state.tasks = data.tasks || []; Trio.store.set('workspace.tasks', state.tasks); markLoaded('tasks'); }),
      api.get('/api/approvals').then(data => { state.approvals = data.approvals || []; Trio.store.set('workspace.approvals', state.approvals); markLoaded('approvals'); }),
      api.get('/api/questions').then(data => { state.questions = data.questions || []; Trio.store.set('workspace.questions', state.questions); markLoaded('questions'); }),
      api.get('/api/mentions').then(data => { state.mentions = data.mentions || []; Trio.store.set('workspace.mentions', state.mentions); markLoaded('mentions'); }),
      api.get('/api/usage').then(data => { state.usage = data; Trio.store.set('workspace.usage', state.usage); markLoaded('usage'); }),
      // Keep state.agents (live/busy/state + context %) fresh on the same 15s +
      // on-message cadence as everything else. The drawer, face-pile, and roster
      // card all read state.agents, which was otherwise only refetched on an
      // Agent-roster page visit — so their status/context went stale in between.
      // agents.refresh() honors the current archived filter and re-renders the
      // roster page if it's open. It's the slowest slice, so it marks ready last.
      (Trio.agents?.refresh?.() || Promise.resolve()).then(() => markLoaded('agents')),
    ];
    // Names in the SAME ORDER as `requests`, so a rejection can be attributed
    // to the slice it came from. Promise.allSettled preserves order, and
    // without this the only record of WHICH endpoint failed was a console
    // warning — which is why the Home health row could not tell "no agents"
    // from "could not ask about agents".
    const SLICES = ['channels', 'dms', 'meta', 'tasks', 'approvals',
                    'questions', 'mentions', 'usage', 'agents'];
    const results = await Promise.allSettled(requests);
    const failures = results.filter(result => result.status === 'rejected');
    failures.forEach(result => console.warn('workspace refresh failed', result.reason));
    state.sliceErrors = {};
    results.forEach((result, i) => {
      if (result.status === 'rejected') state.sliceErrors[SLICES[i]] = result.reason;
    });
    if (failures.length === results.length) state.workspaceError = 'Workspace refresh failed';
    // A rejected slice never ran its markLoaded(), which would otherwise leave
    // its Home section spinning forever (no error, no retry) whenever SOME — but
    // not all — slices fail. Mark every slice settled now: a failed section then
    // renders its empty/last-known state (matching pre-progressive-load
    // behaviour), and the 15s poll refills it if the endpoint recovers.
    state.loaded = state.loaded || {};
    SLICES.forEach(k => { state.loaded[k] = true; });
    state.workspaceLoading = false;
    renderRail();
    renderChannelMetadataState();
    // state.agents was just refreshed above; repaint the face-pile and (if open)
    // the drawer members so their dots reflect the current live/busy/state
    // without waiting for a roster SSE tick.
    renderFacePile();
    refreshDrawerMembers();
    Trio.events.dispatchEvent(new CustomEvent('workspace:updated', {detail: state}));
    if (['home','attention','messages','tasks'].includes(state.view)) showView(state.view);
  }
  let searchDialog = null, searchController = null, searchTimer = null, searchKeydown = null, detailsClick = null;
  function onSearchKey(event) { if ((event.metaKey || event.ctrlKey) && event.key === 'k') { event.preventDefault(); openSearch(); } }
  function renderSearchResults(query, results = []) {
    const list = searchDialog.querySelector('.search-results');
    list.innerHTML = '';
    if (state.searchLoading) { list.innerHTML = '<p class="home-empty">Searching…</p>'; return; }
    if (!query) { list.innerHTML = '<p class="home-empty">Start typing to search.</p>'; return; }
    // "Your workspace contains no match" is a claim about the DATA. It must not
    // be shown when the search never ran — a query the server rejected as too
    // short, or a request that failed, both used to render as "No results.",
    // which is a factual falsehood about the operator's own workspace.
    if (state.searchNotice) { list.innerHTML = `<p class="home-empty">${esc(state.searchNotice)}</p>`; return; }
    if (!results.length) { list.innerHTML = '<p class="home-empty">No results.</p>'; return; }
    const q = query.toLowerCase();
    for (const r of results) {
      const b = document.createElement('button'); b.type = 'button'; b.className = 'search-result';
      const ctx = r.dm ? 'DM · ' + r.dm : '#' + (r.channel || 'unknown');
      const author = r.member_name || r.member_id || 'unknown';
      const time = r.created_at ? new Date(r.created_at).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';
      const escaped = esc(r.content || '');
      const text = q && escaped.toLowerCase().includes(q.toLowerCase()) ? escaped.replace(new RegExp('(' + escRe(q) + ')', 'ig'), '<mark>$1</mark>') : escaped;
      b.innerHTML = `<span class="search-meta">${esc(ctx)} · ${esc(author)} · ${esc(time)}</span><span class="search-body">${text}</span>`;
      b.addEventListener('click', () => { searchDialog.close(); if (r.dm) openDmByKey(r.dm); else openChannel(r.channel); if (r.id != null) setTimeout(() => { const card = document.querySelector(`[data-message-id="${r.id}"]`); if (card) { card.scrollIntoView({ behavior: 'smooth', block: 'center' }); card.focus(); } }, 200); });
      list.append(b);
    }
  }
  function escRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
  // The server requires at least 2 characters and answers 400 below that. The
  // client knows the rule, so it says so instead of issuing a request whose
  // rejection it would then have to interpret.
  const SEARCH_MIN = 2;
  async function doSearch(q) {
    state.searchNotice = '';
    if (!q) { renderSearchResults(''); return; }
    if (q.trim().length < SEARCH_MIN) {
      state.searchNotice = `Type at least ${SEARCH_MIN} characters to search.`;
      renderSearchResults(q, []);
      return;
    }
    state.searchLoading = true; renderSearchResults(q, []);
    if (searchController) { try { searchController.abort(); } catch {} }
    searchController = new AbortController();
    try {
      const resp = await fetch('/api/search?q=' + encodeURIComponent(q) + (state.channel ? '&channel=' + encodeURIComponent(state.channel) : ''), { signal: searchController.signal, headers: { Accept: 'application/json' } });
      if (!resp.ok) throw new Error('search failed');
      const data = await resp.json();
      renderSearchResults(q, data.results || []);
    } catch (e) {
      if (e.name !== 'AbortError') {
        console.warn('search failed', e);
        // A failed search is not an empty workspace. Say which happened.
        state.searchNotice = 'Search is unavailable right now.';
        renderSearchResults(q, []);
      }
    }
    finally { state.searchLoading = false; }
  }
  function channelStatus(member) {
    // An archived agent keeps its placement so unarchiving can restore it, but
    // it is not a live member that happens to be down: say so before any
    // liveness heuristic gets a chance to call it "Offline".
    if (member?.archived) return 'archived';
    // Agent roster objects carry {state, live, busy} from the supervisor —
    // the same source as the Agent roster page. Prefer those when present so
    // the details panel agrees with the roster instead of falling back to the
    // heartbeat-based channel roster status (which reads stale/dead for a
    // sleeping agent because no monitor is running to heartbeat).
    if (member?.live != null && member?.state != null) {
      const rawState = String(member.state).toLowerCase();
      // Compaction is its own state — surface it as "Compacting", not
      // "Sleeping", so the drawer/face-pile agree with the Agent roster card.
      if (rawState === 'compacting') return 'compacting';
      if (member.live) {
        // Working/idle is the fast-changing bit: prefer the roster's
        // member_status (pushed over the SSE roster stream within ~1s) so
        // "working" + the tool chip appear in near-realtime, rather than waiting
        // for the slower /api/agents poll's busy flag.
        const rosterStatus = String(member.status || '').toLowerCase();
        // `blocked` has to be answered before the working/idle split, not
        // folded into it. A blocked session is frozen on an interactive host
        // prompt: it is mid-turn, so it is live and its heartbeats are fresh,
        // but it will sit there indefinitely until a human replies. Because it
        // is neither 'working' nor busy, this branch used to return `idle` for
        // it the moment /api/agents supplied {live,state} — so the drawer, the
        // status chip and the face-pile all described a stuck agent as merely
        // quiet, and the one state that needs a human was the one state the UI
        // hid. member_status() ranks it above `working` for this reason.
        if (rosterStatus === 'blocked') return 'blocked';
        return (rosterStatus === 'working' || member.busy) ? 'working' : 'idle';
      }
      if (rawState === 'error' || rawState === 'errored') return 'errored';
      if (rawState === 'sleeping') return rawState;
      return 'offline';
    }
    const raw = String(member?.status || (member?.busy ? 'working' : member?.live ? 'active' : 'offline')).toLowerCase();
    if (raw === 'error') return 'errored';
    if (raw === 'stale' || raw === 'dead') return 'offline';
    return ['working','blocked','errored','sleeping','active','idle','offline','compacting','archived'].includes(raw) ? raw : 'offline';
  }
  function channelStatusLabel(status) { return status === 'errored' ? 'Errored' : status[0].toUpperCase() + status.slice(1); }
  function channelStatusChip(status) { return `<span class="channel-status-chip ${status}"><span class="dot"></span>${channelStatusLabel(status)}</span>`; }
  // Live "what are they doing" hint sourced from sessions.last_tool_name/
  // last_tool_target (nth_activity_hook) — only meaningful while `working`,
  // since a finished turn's last tool call is stale trivia otherwise.
  function toolSuffix(member, status) {
    if (status !== 'working' || !member?.last_tool_name) return '';
    const target = member.last_tool_target ? `: ${member.last_tool_target}` : '';
    const when = member.last_tool_at ? ` (${timeAgo(member.last_tool_at) || 'now'})` : '';
    return ` — using ${member.last_tool_name}${target}${when}`;
  }
  // A small numeric context-fullness hint, shown wherever a member's context
  // usage is known (nth_supervisor persists it from a Claude Code turn's
  // token usage). Absent for anyone that hasn't completed a turn yet —
  // humans, freshly-spawned agents, or non-Claude providers.
  // LOTC/Frodo: "43% ctx" alone reads as directionally ambiguous (used vs.
  // remaining, battery-meter mental model) — "% full" states the direction
  // in the face of the badge, not just the hover tooltip.
  function contextBadge(member) {
    if (member?.context_pct == null) return '';
    const pct = Math.round(Number(member.context_pct));
    return `<span class="context-badge ${usageTone(pct)}" title="${esc(String(pct))}% of context window used">${esc(String(pct))}% full</span>`;
  }
  // ── Edit-members mode ──────────────────────────────────────────────────────
  // A toggle on the drawer's Members section reveals a per-member remove (×) and
  // an "Add member" affordance. Resets to off each time the drawer opens.
  let editMembersMode = false;
  function operatorId() { return (state.operator || state.meta?.operator)?.id; }
  // The operator (you) and any human participant can't be culled — the server
  // rejects self-removal too, but we don't even offer the affordance.
  function memberIsRemovable(member) {
    return !!member && member.kind !== 'human' && member.id !== operatorId();
  }
  function detailMember(member) {
    const name = member.name || member.id || 'Unknown member';
    const status = channelStatus(member);
    // An archived agent's last status_text ("idle — standing by") is stale and
    // reads as if it were still working; the archive fact outranks it.
    const statusText = status === 'archived' ? 'Archived — restore to rejoin'
      : (member.status_text || member.statusText
         || (status === 'active' ? 'Active in this channel' : channelStatusLabel(status)));
    const hint = toolSuffix(member, status).replace(/^ — /, '');
    const tool = hint ? `<div class="channel-member-tool">${esc(hint)}</div>` : '';
    const removable = editMembersMode && memberIsRemovable(member);
    const removeBtn = removable
      ? `<button type="button" class="icon-btn danger member-remove" data-remove-member="${esc(member.id)}" data-member-name="${esc(name)}" aria-label="Remove ${esc(name)} from channel" title="Remove from channel">${navIcon('x')}</button>`
      : '';
    // Subagents (#6): agents can spawn subagents (Task/Agent tool calls). Show
    // an agent member's recently-spawned ones here — the box is filled async by
    // hydrateDrawerSubagents(). Humans / the operator don't spawn subagents.
    const opId = state.operator?.id || state.meta?.operator?.id;
    const isAgent = member.kind !== 'human' && member.id !== opId;
    const subagents = isAgent ? `<div class="channel-member-subagents" data-subagents-for="${esc(member.id)}"></div>` : '';
    return `<div class="channel-member${status === 'archived' ? ' is-archived' : ''}">${avatarFor(member, status)}<div class="channel-member-copy"><div class="channel-member-name">${esc(name)}</div><div class="channel-member-status">${esc(statusText)}</div>${tool}${subagents}</div>${contextBadge(member)}${channelStatusChip(status)}${removeBtn}</div>`;
  }
  // Subagent list under an agent's drawer row. "Recent spawns" — tool_events
  // records Task/Agent starts only (no completion), so this is honestly labelled
  // recent, not a guaranteed-live count. Capped so a busy agent can't flood.
  const SUBAGENT_TTL = 15000;
  const subagentCache = new Map();   // member id -> { at, items }
  const subagentPending = new Set();
  function subagentsFromResponse(data) {
    return Array.isArray(data?.subagents) ? data.subagents : [];
  }
  function renderSubagentList(items) {
    if (!items || !items.length) return '';
    const MAX = 4;
    const rows = items.slice(0, MAX).map(s =>
      `<div class="subagent-row"><span class="sa-arrow" aria-hidden="true">↳</span><span class="sa-name">${esc(s.target || s.tool_name || 'subagent')}</span><span class="sa-time">${esc(timeAgo(s.created_at) || 'now')}</span></div>`).join('');
    const more = items.length > MAX ? `<div class="subagent-more">+${items.length - MAX} earlier</div>` : '';
    return `<div class="subagent-head">Recent subagents</div>${rows}${more}`;
  }
  function hydrateDrawerSubagents() {
    const boxes = document.querySelectorAll('#channel-drawer-members [data-subagents-for]');
    const now = Date.now();
    boxes.forEach(box => {
      const id = box.getAttribute('data-subagents-for');
      const cached = subagentCache.get(id);
      if (cached) { box.innerHTML = renderSubagentList(cached.items); if (now - cached.at < SUBAGENT_TTL) return; }
      if (subagentPending.has(id)) return;
      subagentPending.add(id);
      Trio.api.get('/api/tools?member=' + encodeURIComponent(id))
        .then(subagentsFromResponse)
        // A failure caches an empty list exactly like a success does. The
        // subagent feed is written by the activity hook, which is not part of
        // every deployment. A request can also fail while the hub restarts;
        // caching only the success path would then re-request on every single
        // drawer render forever. An empty list renders nothing, and the TTL
        // means the client starts showing subagents after recovery/install.
        .catch(() => [])
        .then(items => {
          subagentCache.set(id, { at: Date.now(), items });
          if (box.isConnected !== false) box.innerHTML = renderSubagentList(items);
        })
        .finally(() => subagentPending.delete(id));
    });
  }
  async function removeMember(id, name) {
    try {
      await Trio.api.post('/api/cull', { target_member_id: id });
      state.members?.delete?.(id);          // optimistic — SSE roster reconciles
      refreshDrawerMembers();
      toast(`Removed ${name} from #${state.channel}`);
    } catch (error) {
      toast(error.message || 'Could not remove member');
    }
  }
  // Removing a member is destructive: culling a mid-work agent releases the tasks
  // it has claimed, which re-adding cannot restore. So confirm when the member is
  // actively working/blocked; an idle member stays a snappy one-click remove.
  function requestRemoveMember(id, name) {
    const { members } = drawerMembers();
    const member = members.find(m => m.id === id);
    const status = member ? channelStatus(member) : 'idle';
    if (status === 'working' || status === 'blocked' || status === 'active') {
      Trio.ui.confirmAction(
        `Remove ${name} from #${state.channel}?`,
        `${name} is ${status} — removing them releases any tasks they've claimed.`,
        () => removeMember(id, name),
        { submitLabel: 'Remove', danger: true });
    } else {
      removeMember(id, name);
    }
  }
  // Add managed agents to this channel via the same placement endpoint the Agent
  // Roster uses. Candidates = managed agents not already present here.
  function openAddMember() {
    const current = new Set([...(state.members?.keys?.() || [])]);
    const opId = operatorId();
    const candidates = agentArray().filter(a => a.id && a.id !== opId && !current.has(a.id));
    if (!candidates.length) { toast('Every agent is already in this channel'); return; }
    const rows = candidates.map(a =>
      `<label class="check-row"><input type="checkbox" name="add-member" value="${esc(a.id)}"><span>${esc(a.name || a.id)}</span></label>`).join('');
    const nameById = new Map(candidates.map(a => [a.id, a.name || a.id]));
    Trio.ui.modal('Add members to #' + (state.channel || ''), `<div class="check-list">${rows}</div>`, async node => {
      const ids = [...node.querySelectorAll('input[name="add-member"]:checked')].map(i => i.value);
      if (!ids.length) return;
      let added = 0;
      for (const id of ids) {
        try {
          await Trio.api.post(`/api/agents/${encodeURIComponent(id)}/placement`, { channel: state.channel, present: true });
          // Optimistically seed the roster so the drawer + count update now; the
          // SSE roster tick reconciles with the full member row within ~1s.
          state.members?.set?.(id, { id, name: nameById.get(id) || id });
          added++;
        } catch (error) { toast(error.message || `Could not add ${id}`); }
      }
      if (added) { refreshDrawerMembers(); toast(`Added ${added} member${added > 1 ? 's' : ''} to #${state.channel}`); }
    });
  }
  function toggleEditMembers() {
    editMembersMode = !editMembersMode;
    const btn = $('edit-members-toggle');
    if (btn) {
      btn.classList.toggle('is-active', editMembersMode);
      btn.setAttribute('aria-pressed', String(editMembersMode));
      btn.title = editMembersMode ? 'Done editing members' : 'Edit members';
    }
    const add = $('add-member-btn');
    if (add) add.hidden = !editMembersMode;
    refreshDrawerMembers();
  }
  function detailTask(task) {
    const status = ['open','claimed','blocked','done'].includes(task.status) ? task.status : 'open';
    const title = task.title || task.description || task.message || 'Task';
    return `<div class="channel-task"><span class="tstate ${status}"><span class="d"></span>${esc(status)}</span><span class="channel-task-text">#${esc(task.id)} ${esc(title)}</span></div>`;
  }
  function closeDetails() {
    const drawer = $('channel-drawer');
    if (!drawer) return;
    drawer.classList.remove('open'); $('app')?.classList.remove('channel-details-open'); drawer.setAttribute('aria-hidden', 'true'); $('details-btn')?.classList.remove('menu-active');
  }
  function startDrawerResize(event) {
    if (event.button !== 0) return;
    const app = $('app'); const drawer = $('channel-drawer'); const handle = $('channel-drawer-resize');
    if (!app || !drawer || !handle) return;
    event.preventDefault();
    const startWidth = drawer.getBoundingClientRect().width;
    const startX = event.clientX;
    const minWidth = 320;
    const maxWidth = Math.min(560, Math.max(minWidth, window.innerWidth - 420));
    app.classList.add('is-resizing');
    drawerResizeStart = {startWidth, startX, minWidth, maxWidth};
    drawerResizeMove = move => {
      if (!drawerResizeStart) return;
      const width = Math.round(Math.max(drawerResizeStart.minWidth, Math.min(drawerResizeStart.maxWidth, drawerResizeStart.startWidth + drawerResizeStart.startX - move.clientX)));
      app.style.setProperty('--channel-drawer-width', width + 'px');
      handle.setAttribute('aria-valuenow', String(width));
    };
    drawerResizeEnd = () => {
      drawerResizeStart = null; app.classList.remove('is-resizing');
      document.removeEventListener('pointermove', drawerResizeMove); document.removeEventListener('pointerup', drawerResizeEnd);
    };
    document.addEventListener('pointermove', drawerResizeMove); document.addEventListener('pointerup', drawerResizeEnd, {once:true});
  }
  // Compute the drawer's member rows from current state — extracted from
  // showDetails so refreshDrawerMembers() can recompute them live on a roster
  // tick without rebuilding the whole panel. Each roster member carries the
  // SSE-pushed member_status + tool + context (fresh within ~1s); state.agents
  // overlays the supervisor-backed {live,busy,state}. channelStatus() then reads
  // working/idle from the fresh roster status and connected/sleeping from the
  // agent fields.
  function drawerMembers() {
    // Resolve from state.dmThread first (the authoritative object openDm stashed,
    // cleared on channel nav): your_dms omits audit (agent-to-agent) and archived
    // threads, so a your_dms-only lookup returned null for them and the drawer
    // mislabeled a real DM as a channel ("Channel size", "#__agent_inbox__").
    const dm = state.dmKey ? (state.dmThread || (state.dms?.your_dms || []).find(d => d.key === state.dmKey)) : null;
    const allMembers = [...(state.members?.values?.() || [])];
    const agentsById = new Map(agentArray().map(a => [a.id, a]));
    // Rendering this drawer at all means the operator's own client is live, so
    // mark self-presence true (their raw identity object carries no liveness).
    const rawOperator = state.operator || state.meta?.operator;
    const operator = rawOperator ? { ...rawOperator, live: true, state: rawOperator.state || 'running', busy: false } : rawOperator;
    const mergeRosterInfo = member => {
      const agent = agentsById.get(member.id);
      if (agent) return { ...member, ...agent };
      return operator?.id === member.id ? { ...member, ...operator } : member;
    };
    const resolveDmMember = id => {
      const agent = agentsById.get(id);
      const rosterM = allMembers.find(m => m.id === id);
      if (agent) return { ...rosterM, ...agent };
      return rosterM || (id === operator?.id ? operator : { id, name: id });
    };
    const dmIds = Array.isArray(state.dmMemberIds) ? state.dmMemberIds.slice() : [];
    if (operator?.id && !dmIds.includes(operator.id)) dmIds.push(operator.id);
    const members = dm && dmIds.length
      ? dmIds.map(resolveDmMember)
      : (() => {
          const merged = allMembers.map(mergeRosterInfo);
          if (operator?.id && !merged.some(m => m.id === operator.id)) merged.push(operator);
          // Archived agents sink to the bottom: they're kept for restore, not
          // part of who's actually in the room.
          return merged.sort((a, b) => Number(!!a.archived) - Number(!!b.archived));
        })();
    const channel = (state.channels || []).find(c => c.code === state.channel);
    const count = (dm && dmIds.length) ? dmIds.length : (members.length || Number(channel?.members) || 0);
    return { members, count };
  }
  // Live-refresh just the drawer's Members section on a roster/agent tick so an
  // OPEN drawer reflects status/tool changes in near-realtime without tearing
  // down the whole panel (which would re-run the channel-size fetch and reset
  // scroll). No-op when the drawer is closed.
  function refreshDrawerMembers() {
    const drawer = $('channel-drawer');
    if (!drawer || !drawer.classList.contains('open')) return;
    const list = $('channel-drawer-members');
    if (!list) return;
    const { members, count } = drawerMembers();
    list.innerHTML = members.length ? members.map(detailMember).join('') : '<div class="channel-drawer-empty">Waiting for the current roster…</div>';
    const heading = $('channel-drawer-members-heading');
    if (heading) heading.textContent = 'Members · ' + count;
    hydrateDrawerSubagents();
  }
  function showDetails(refresh = false) {
    // Only an explicit `true` means "re-render an already-open drawer" (the
    // openChannel-on-conversation-change caller). mount() binds showDetails
    // directly as the details-btn click handler, so a click passes the Event
    // as `refresh`; without this coercion that truthy Event trips the
    // early-return below and the drawer never opens (regression from ff105ad).
    refresh = refresh === true;
    const drawer = $('channel-drawer'); const body = $('channel-drawer-body');
    if (!drawer || !body) return;
    // On a refresh (conversation changed while the drawer was already open),
    // don't yank focus back to the close button — the user is navigating.
    if (refresh && !drawer.classList.contains('open')) return;
    closeChannelMenu();
    const channel = (state.channels || []).find(c => c.code === state.channel);
    const archived = !!channel?.archived || !!state.readOnly;
    // Resolve from state.dmThread first (the authoritative object openDm stashed,
    // cleared on channel nav): your_dms omits audit (agent-to-agent) and archived
    // threads, so a your_dms-only lookup returned null for them and the drawer
    // mislabeled a real DM as a channel ("Channel size", "#__agent_inbox__").
    const dm = state.dmKey ? (state.dmThread || (state.dms?.your_dms || []).find(d => d.key === state.dmKey)) : null;
    const title = dm ? (dm.name || state.dmKey) : '#' + (state.channel || 'nth');
    // Membership editing applies to real channels only (not DMs) and not when
    // archived/read-only. Always start with edit mode off on a fresh open.
    const canEditMembers = !dm && !archived;
    editMembersMode = false;
    const { members, count: memberCount } = drawerMembers();
    const tasks = selectors.taskItems().filter(task => !task.channel || task.channel === state.channel).filter(task => task.status !== 'done' && task.status !== 'cancelled');
    const connection = $('h-conn')?.querySelector('.conn-label')?.textContent || (archived ? 'Archived' : 'Live');
    $('channel-drawer-title').textContent = title;
    body.innerHTML = `<section class="channel-drawer-section"><h3>Topic</h3><div class="channel-drawer-topic">${esc(channel?.topic || (dm ? 'Private conversation' : 'No topic'))}</div></section><section class="channel-drawer-section channel-members-section"><h3><span id="channel-drawer-members-heading">Members · ${memberCount}</span>${canEditMembers ? `<button type="button" class="icon-btn edit-members-toggle" id="edit-members-toggle" aria-label="Edit members" aria-pressed="false" title="Edit members">${navIcon('edit')}</button>` : ''}</h3><div id="channel-drawer-members">${members.length ? members.map(detailMember).join('') : '<div class="channel-drawer-empty">Waiting for the current roster…</div>'}</div>${canEditMembers ? `<button type="button" class="btn ghost add-member-btn" id="add-member-btn" hidden>${navIcon('plus')}<span>Add member</span></button>` : ''}</section><section class="channel-drawer-section"><h3>Tasks · ${tasks.length}</h3>${tasks.length ? tasks.slice(0, 4).map(detailTask).join('') : '<div class="channel-drawer-empty">No open tasks.</div>'}${tasks.length > 4 ? '<div class="channel-drawer-empty">+' + (tasks.length - 4) + ' more tasks</div>' : ''}<button type="button" class="btn ghost" id="open-channel-tasks">Open tasks view</button></section><section class="channel-drawer-section"><h3>Activity</h3><div class="kv"><span class="k">Messages loaded</span><span class="v" id="channel-drawer-msgcount" title="Capped at the most recent 500 — not literally every message in this conversation's history">${messageCountLabel()}</span></div><div class="kv"><span class="k">${dm ? 'Conversation size' : 'Channel size'}</span><span class="v" id="channel-drawer-size" title="Rough estimate of this ${dm ? 'conversation' : 'channel'}'s message-history size — a different measurement than an individual agent's own context-fullness badge above">…</span></div><div class="kv"><span class="k">Connection</span><span class="v live">${esc(connection)}</span></div></section><section class="channel-drawer-section"><h3>${dm ? 'Conversation' : 'Channel'}</h3><div class="channel-drawer-actions"><button type="button" class="btn" id="edit-channel-objective">${dm ? 'Conversation settings' : 'Edit objective'}</button>${state.channel ? `<button type="button" class="btn danger" id="archive-channel-drawer">${dm ? (archived ? 'Restore conversation' : 'Archive conversation') : (archived ? 'Restore channel' : 'Archive channel')}</button>` : ''}</div></section>`;
    $('app')?.classList.add('channel-details-open'); drawer.classList.add('open'); drawer.setAttribute('aria-hidden', 'false'); $('details-btn')?.classList.add('menu-active');
    if (!refresh) $('channel-drawer-close')?.focus();
    $('open-channel-tasks')?.addEventListener('click', () => { closeDetails(); navigateView('tasks'); });
    $('edit-members-toggle')?.addEventListener('click', toggleEditMembers);
    $('add-member-btn')?.addEventListener('click', openAddMember);
    // Delegated so it survives refreshDrawerMembers() repaints of the list.
    $('channel-drawer-members')?.addEventListener('click', event => {
      const btn = event.target.closest('[data-remove-member]');
      if (!btn) return;
      requestRemoveMember(btn.getAttribute('data-remove-member'), btn.getAttribute('data-member-name') || 'member');
    });
    $('edit-channel-objective')?.addEventListener('click', () => toast('Objective editing is coming soon'));
    $('archive-channel-drawer')?.addEventListener('click', () => { closeDetails(); archiveCurrent(); });
    refreshDrawerActivity();
    hydrateDrawerSubagents();
  }
  // LOTC/Frodo: "Messages loaded" is exactly what state.messages.size is —
  // the currently in-memory set, capped at 11-conversation.js's
  // pruneMessages(500) — NOT literally "today" (no date filter exists) and
  // not the conversation's full history. A busy channel pins at 500 and
  // stops moving even as more arrive; showing "500+" instead of a frozen
  // "500" makes that cap visible instead of looking broken.
  const LOADED_MESSAGES_CAP = 500;
  function messageCountLabel() {
    const size = state.messages?.size || 0;
    return size >= LOADED_MESSAGES_CAP ? `${LOADED_MESSAGES_CAP}+` : String(size);
  }
  // 2-significant-figure K/M formatting (1.2K, 12K, 120K, 1.2M) — avoids
  // Number.toPrecision's exponential-notation quirk for 3-digit values
  // (e.g. (123).toPrecision(2) === "1.2e+2", not "120").
  function roundToSigFigs(value, figs) {
    if (!value) return 0;
    const magnitude = Math.pow(10, figs - Math.ceil(Math.log10(Math.abs(value))));
    return Math.round(value * magnitude) / magnitude;
  }
  const CHANNEL_SIZE_WARN_TOKENS = 850000;
  function formatTokenEstimate(tokens) {
    const n = Number(tokens) || 0;
    if (n >= 1e6) return roundToSigFigs(n / 1e6, 2) + 'M';
    if (n >= 1e3) return roundToSigFigs(n / 1e3, 2) + 'K';
    return String(Math.round(n));
  }
  const WARNING_TRIANGLE_SVG = '<svg class="size-warn-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-label="Approaching context limit"><path d="m10.29 3.86-8.18 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.89-3.14l-8.18-14a2 2 0 0 0-3.42 0Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>';
  let drawerActivityFetchToken = 0;
  // Live-refresh both the message count (from already-in-memory state — no
  // request needed) and the channel-size estimate (a cheap aggregate) every
  // time this is called. Previously "Messages today" only reflected
  // whatever was true at the moment the drawer happened to open — a message
  // arriving while it stayed open just sat there stale until close+reopen.
  async function refreshDrawerActivity(messageChannel) {
    const drawer = $('channel-drawer');
    if (!drawer || !drawer.classList.contains('open')) return;
    const countEl = $('channel-drawer-msgcount');
    if (countEl) countEl.textContent = messageCountLabel();
    const sizeEl = $('channel-drawer-size');
    const isDmView = !!state.dmKey;
    if (!sizeEl || (!isDmView && !state.channel)) return;
    // LOTC/Legolas: 'message' fires for the workspace-wide stream, not just
    // the open channel — a message anywhere used to refire this fetch for
    // whatever channel's drawer happened to be open. Skip the network
    // round-trip when we KNOW the event was for a different channel; a
    // missing/ambiguous channel field (or an explicit call with none, e.g.
    // the drawer's own open-time refresh) still fetches, since that's the
    // safe default. DM size is keyed by thread (dmKey), not channel, so this
    // channel-scoped guard doesn't apply to a DM view.
    if (!isDmView && messageChannel != null && messageChannel !== state.channel) return;
    const fetchToken = ++drawerActivityFetchToken;
    try {
      const query = isDmView
        ? 'dm=' + encodeURIComponent(state.dmKey)
        : 'channel=' + encodeURIComponent(state.channel);
      const data = await api.get('/api/channel-size?' + query);
      // The drawer may have closed, or moved to a different channel/DM,
      // while this request was in flight — a stale response landing after
      // must not overwrite what's now on screen.
      if (fetchToken !== drawerActivityFetchToken) return;
      const el = $('channel-drawer-size');
      if (!el) return;
      const tokens = data.estimated_tokens || 0;
      const warn = tokens > CHANNEL_SIZE_WARN_TOKENS;
      el.title = 'Rough estimate — message + sender text ÷ 4, plus per-message JSON overhead. Not an exact tokenizer count.';
      el.innerHTML = `${esc(formatTokenEstimate(tokens))} tokens (est.)${warn ? ` <span title="Approaching ${esc(formatTokenEstimate(CHANNEL_SIZE_WARN_TOKENS))} tokens — agents' context may compact soon">${WARNING_TRIANGLE_SVG}</span>` : ''}`;
      el.classList.toggle('size-warn', warn);
    } catch (e) {
      if (fetchToken !== drawerActivityFetchToken || !sizeEl) return;
      // LOTC/Frodo: collapsing every failure (403/404/500) to the same bare
      // "—" left a scoped/guest viewer unable to tell "forbidden" apart from
      // "broken" apart from "empty". At minimum distinguish the one case
      // that's a normal, expected outcome (not authorized for this channel)
      // from a genuine error.
      sizeEl.textContent = e?.status === 403 ? 'not visible to you' : 'unavailable';
      sizeEl.title = '';
    }
  }
  // Live workspace refresh on incoming messages. Without this, the rail unread
  // badges (/api/channels) and the Home cards (Attention inbox / Messages for
  // you / Tasks in flight, from /api/mentions|approvals|questions) only updated
  // on the 15s setInterval — so a message arriving in another channel didn't
  // bump its unread bubble or the Home counts until the next poll. The
  // workspace-wide SSE stream already delivers every channel's messages to the
  // operator, so re-poll (debounced, to collapse bursts / the prime flood) when
  // one lands. refresh() itself no-ops if a poll is already in flight; the 15s
  // interval remains a backstop.
  let liveRefreshDebounce = null;
  function onMessageLiveRefresh() {
    clearTimeout(liveRefreshDebounce);
    liveRefreshDebounce = setTimeout(() => { refresh(); }, 600);
  }
  let drawerActivityDebounce = null;
  function onMessageForDrawer(event) {
    clearTimeout(drawerActivityDebounce);
    const messageChannel = event?.detail?.channel;
    drawerActivityDebounce = setTimeout(() => refreshDrawerActivity(messageChannel), 400);
  }
  function openSearch() {
    if (!searchDialog) { searchDialog = document.createElement('dialog'); searchDialog.id = 'trio-search'; searchDialog.className = 'search-modal'; document.body.append(searchDialog); }
    Trio.ui.configureDialog(searchDialog);
    searchDialog.innerHTML = '<form method="dialog"><button type="submit" formnovalidate class="modal-close" value="cancel">×</button><input class="search-input" placeholder="Search messages…" aria-label="Search"><div class="search-results"></div></form>';
    searchDialog.showModal();
    const input = searchDialog.querySelector('.search-input');
    input.focus();
    input.addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => doSearch(input.value.trim()), 200); });
  }
  let refreshInterval = null;
  let agentsInterval = null;
  // Held across a mount so it can be removed again. Otherwise rotating a phone
  // leaves the old face cap until an unrelated roster event or agent poll.
  let faceMedia = null;
  let faceMediaChange = null;
  // Dedicated faster poll for agent live/busy/state (+context) so connected/
  // sleeping/compacting transitions show within ~5s — cheaper bits than the full
  // 15s workspace refresh, operator-gated, no agent tokens. Working/idle + tool
  // use come faster still, over the roster SSE (see renderFacePile/refreshDrawerMembers).
  function pollAgents() { return (Trio.agents?.refresh?.() || Promise.resolve()).then(() => { renderFacePile(); refreshDrawerMembers(); }); }
  // Feed ownership is per mount and is surrendered on unmount; otherwise a
  // later boot could skip core's fallback using evidence from an old lifetime.
  function mountFaceMedia() {
    try { faceMedia = window.matchMedia?.(FACE_MEDIA) || null; } catch { faceMedia = null; }
    if (!faceMedia) return;
    faceMediaChange = () => renderFacePile();
    faceMedia.addEventListener?.('change', faceMediaChange);
  }
  function unmountFaceMedia() {
    if (faceMedia && faceMediaChange) faceMedia.removeEventListener?.('change', faceMediaChange);
    faceMedia = null; faceMediaChange = null;
  }
  function mount() { routeFeedClaimed = false; mountFaceMedia(); refresh(); renderFacePile(); if (!refreshInterval) refreshInterval = setInterval(refresh, 15000); if (!agentsInterval) agentsInterval = setInterval(pollAgents, 5000); unroute = Trio.router?.on?.(onRoute); wsl = onWorkspaceUpdate; Trio.events?.addEventListener?.('workspace:updated', wsl); preferencesChanged = onPreferencesChanged; Trio.events?.addEventListener?.('preferences:changed', preferencesChanged); Trio.events?.addEventListener?.('roster', renderFacePile); Trio.events?.addEventListener?.('roster', refreshDrawerMembers); Trio.events?.addEventListener?.('message', onMessageForDrawer); Trio.events?.addEventListener?.('message', onMessageLiveRefresh); const searchBtn = $('search-btn'); if (searchBtn) { searchBtn.addEventListener('click', openSearch); } const detailsBtn = $('details-btn'); if (detailsBtn) { detailsClick = showDetails; detailsBtn.addEventListener('click', detailsClick); } const drawerClose = $('channel-drawer-close'); if (drawerClose) drawerClose.addEventListener('click', closeDetails); const drawerResize = $('channel-drawer-resize'); if (drawerResize) drawerResize.addEventListener('pointerdown', startDrawerResize); const menuButton = $('channel-more-btn'); if (menuButton) { menuButtonClick = openChannelMenu; menuButton.addEventListener('click', menuButtonClick); } const accountTrigger = $('account-trigger'); if (accountTrigger) { accountTriggerClick = openAccountMenu; accountTrigger.addEventListener('click', accountTriggerClick); } menuClick = event => { if (!event.target.closest('#channel-menu, #channel-more-btn')) closeChannelMenu(); if (!event.target.closest('#account')) closeAccountMenu(); }; menuKeydown = event => { if (event.key === 'Escape') { closeChannelMenu(); closeDetails(); closeAccountMenu(); } }; document.addEventListener('click', menuClick); document.addEventListener('keydown', menuKeydown); searchKeydown = onSearchKey; document.addEventListener('keydown', searchKeydown); }
  function unmount() { routeFeedClaimed = false; unmountFaceMedia(); closeChannelMenu(); closeDetails(); if (drawerResizeEnd) drawerResizeEnd(); if (refreshInterval) { clearInterval(refreshInterval); refreshInterval = null; } if (agentsInterval) { clearInterval(agentsInterval); agentsInterval = null; } if (unroute) { unroute(); unroute = null; } if (wsl) { Trio.events?.removeEventListener?.('workspace:updated', wsl); wsl = null; } if (preferencesChanged) { Trio.events?.removeEventListener?.('preferences:changed', preferencesChanged); preferencesChanged = null; } Trio.events?.removeEventListener?.('roster', renderFacePile); Trio.events?.removeEventListener?.('roster', refreshDrawerMembers); Trio.events?.removeEventListener?.('message', onMessageForDrawer); Trio.events?.removeEventListener?.('message', onMessageLiveRefresh); clearTimeout(liveRefreshDebounce); clearTimeout(drawerActivityDebounce); const searchBtn = $('search-btn'); if (searchBtn && openSearch) searchBtn.removeEventListener('click', openSearch); const detailsBtn = $('details-btn'); if (detailsBtn && detailsClick) detailsBtn.removeEventListener('click', detailsClick); const drawerClose = $('channel-drawer-close'); if (drawerClose) drawerClose.removeEventListener('click', closeDetails); const drawerResize = $('channel-drawer-resize'); if (drawerResize) drawerResize.removeEventListener('pointerdown', startDrawerResize); const menuButton = $('channel-more-btn'); if (menuButton && menuButtonClick) menuButton.removeEventListener('click', menuButtonClick); const accountTrigger = $('account-trigger'); if (accountTrigger && accountTriggerClick) accountTrigger.removeEventListener('click', accountTriggerClick); closeAccountMenu(); if (menuClick) document.removeEventListener('click', menuClick); if (menuKeydown) document.removeEventListener('keydown', menuKeydown); if (searchKeydown) document.removeEventListener('keydown', searchKeydown); }
  Trio.workspace = {init: mount, mount, unmount, render: renderRail, renderFacePile, facePileModel,
    channelMetadataSettled, channelMetadataReady, renderChannelMetadataState,
    didClaimRouteFeed: () => routeFeedClaimed, refresh, archive, archiveCurrent, openChannel, openDm, openDmByKey, openDmDialog, dmTargets, groupNavigation, isStaleThread, staleThreadDays, attentionCount, selectors, showView, search: openSearch, doSearch, modal, toast, showDetails, channelStatus, toolSuffix, usageTone, resetLabel, contextBadge, formatTokenEstimate, refreshDrawerActivity, refreshDrawerMembers, messageCountLabel, createChannel, openTaskModal, detailMember, renderSubagentList, subagentsFromResponse, openAccountMenu, closeAccountMenu, dismissQuestion, undismissQuestion, isQuestionDismissed, trendChip, dailyChangeLine, projectionLine};
})();
