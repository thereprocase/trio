(() => {
  'use strict';
  // Reload and update for the installed app.
  //
  // Installed as an Android WebAPK (display: standalone) the dashboard has no
  // browser chrome: no reload button, no address bar. Chrome's own
  // pull-to-refresh cannot stand in for one here either, because it only
  // fires when the DOCUMENT is pulled at its top, and this document never
  // scrolls: body is overflow:hidden and the conversation scrolls inside
  // .messages (10-shell.css, 20-conversation.css). So the app provides the
  // three pieces itself:
  //
  //   * a pull-to-refresh gesture on touch devices in installed mode;
  //   * a "Update available — Reload" pill when the hub serves a newer build;
  //   * a permanent "Reload app" item in the account menu (20-workspace.js).
  //
  // A reload IS the update. sw.js has no fetch handler and no cache and the
  // page is served no-store, so a reload always fetches the newest bundle.
  // What a reload can keep is the place (the channel or DM is in the URL) and
  // the unsent text, which this module carries across in sessionStorage.
  const Trio = window.Trio;
  if (!Trio) throw new Error('App refresh requires Trio core');
  const { state } = Trio;
  const toast = (...args) => Trio.ui?.toast?.(...args);

  // ── Drafts across a reload ────────────────────────────────────────────
  // The composer keeps drafts in memory only (state.drafts, 12-composer.js),
  // so any reload, ours or the browser's, used to throw the text away. They
  // are written on the way out and read back at definition time, which runs
  // before boot() mounts the composer, so its first loadDraft() finds them.
  const DRAFTS_KEY = 'nth.reload-drafts.v1';
  // A snapshot older than this belongs to some earlier visit, not to the
  // reload that is starting the page now.
  const DRAFTS_MAX_AGE_MS = 10 * 60 * 1000;
  function session() {
    try { return window.sessionStorage || null; } catch { return null; }
  }
  // The composer's own key, so a saved draft lands back in the conversation
  // it was typed in. The fallback is the same rule, for a page without one.
  function conversationKey() {
    const fromComposer = Trio.composer?.conversationId?.();
    if (fromComposer) return fromComposer;
    return state.dmKey ? 'dm:' + state.dmKey : (state.channel || 'home');
  }
  function saveDrafts() {
    const store = session();
    if (!store) return false;
    const drafts = {};
    for (const [key, text] of Object.entries(state.drafts || {})) {
      if (typeof text === 'string' && text.trim()) drafts[key] = text;
    }
    // The box itself is the freshest copy of the open conversation's text.
    const live = document.getElementById('input')?.textContent || '';
    if (live.trim()) drafts[conversationKey()] = live;
    const targets = {};
    for (const [key, ids] of Object.entries(state.targetDrafts || {})) {
      if (Array.isArray(ids) && ids.length) targets[key] = ids.slice();
    }
    try {
      if (!Object.keys(drafts).length && !Object.keys(targets).length) store.removeItem(DRAFTS_KEY);
      else store.setItem(DRAFTS_KEY, JSON.stringify({ at: Date.now(), drafts, targets }));
      return true;
    } catch { return false; }
  }
  function restoreDrafts() {
    const store = session();
    if (!store) return 0;
    let saved = null;
    try { saved = JSON.parse(store.getItem(DRAFTS_KEY) || 'null'); } catch { saved = null; }
    try { store.removeItem(DRAFTS_KEY); } catch { /* read-only storage: nothing to consume */ }
    if (!saved || typeof saved !== 'object' || !(Date.now() - Number(saved.at) < DRAFTS_MAX_AGE_MS)) return 0;
    state.drafts = state.drafts || {};
    state.targetDrafts = state.targetDrafts || {};
    let restored = 0;
    for (const [key, text] of Object.entries(saved.drafts || {})) {
      if (typeof text === 'string' && !state.drafts[key]) { state.drafts[key] = text; restored++; }
    }
    for (const [key, ids] of Object.entries(saved.targets || {})) {
      if (Array.isArray(ids) && !(state.targetDrafts[key] || []).length) state.targetDrafts[key] = ids.map(String);
    }
    return restored;
  }
  restoreDrafts();
  // A page restored from the back/forward cache never reloaded: its drafts
  // are still in memory, and the copy pagehide wrote would otherwise wait for
  // the next real reload and bring back text sent in the meantime.
  function onPageShow(event) {
    if (!event?.persisted) return;
    try { session()?.removeItem(DRAFTS_KEY); } catch { /* nothing to clear */ }
  }

  // ── Reload ────────────────────────────────────────────────────────────
  // Some work cannot be carried through a reload, so the reload is refused
  // with the reason instead of losing it: images waiting in the composer
  // (File objects in memory) and dictation that is still listening or whose
  // recording is still at /api/stt/transcribe.
  function unsentImages() {
    const lists = Object.values(state.attachmentStore || {});
    if (!lists.length && Array.isArray(state.pendingAttachments)) lists.push(state.pendingAttachments);
    return lists.reduce((sum, list) => sum + (Array.isArray(list) ? list.length : 0), 0);
  }
  function reloadBlocker() {
    const images = unsentImages();
    if (images === 1) return 'An image is still waiting to be sent. Send or remove it first: reloading would lose it.';
    if (images) return images + ' images are still waiting to be sent. Send or remove them first: reloading would lose them.';
    const dictation = Trio.composer?.dictationState?.() || '';
    if (dictation === 'recording') return 'Dictation is still listening. Stop it first, then reload.';
    if (dictation === 'transcribing') return 'Your dictation is still being transcribed. Reload once its text appears in the box.';
    if (dictation === 'kept') return 'A recording is kept for Retry. Retry it, use browser dictation, or discard it first: reloading would lose it.';
    return '';
  }
  const WORKER_WAIT_MS = 1500;
  // Reloading while the hub is unreachable replaces the app with the
  // browser's offline page: sw.js has no fetch handler to serve anything
  // else. So the reload first asks the hub, briefly.
  const REACH_TIMEOUT_MS = 2000;
  const CHECK_TIMEOUT_MS = 5000;
  // Settles with the promise's value, or undefined after `ms`. A rejection,
  // early or late, also settles as undefined, so nothing is left unhandled.
  function within(promise, ms) {
    let timer;
    const settled = Promise.resolve(promise).catch(() => undefined);
    const timeout = new Promise(resolve => { timer = setTimeout(resolve, ms); });
    return Promise.race([settled, timeout]).finally(() => clearTimeout(timer));
  }
  function timeoutSignal(ms) {
    try { if (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) return AbortSignal.timeout(ms); } catch { /* fall through */ }
    try { const controller = new AbortController(); setTimeout(() => controller.abort(), ms); return controller.signal; } catch { return undefined; }
  }
  // The build the hub serves now, or null when it cannot be reached in time.
  async function fetchBuild(ms) {
    try {
      const response = await within(fetch('/api/version', { cache: 'no-store', signal: timeoutSignal(ms) }), ms);
      // An older hub may lack this endpoint, or a proxy may require auth.
      // Either 4xx proves reachability, without claiming a build is known.
      if (response?.status >= 400 && response.status < 500) return '';
      if (!response?.ok) return null;
      const data = await within(response.json(), ms);
      return typeof data?.build === 'string' ? data.build : null;
    } catch { return null; }
  }
  // What the page can and cannot refresh about the installed app:
  //   * the bundle: always, by reloading (see the header comment);
  //   * the service worker: registration.update() asks the browser to fetch
  //     sw.js now instead of on its own schedule. sw.js skips waiting, so a
  //     changed worker takes over at once. Bounded so a stalled update never
  //     blocks the reload;
  //   * the manifest, icons and app name: NOT from the page. The installed
  //     app's icon and name belong to the WebAPK, and Chrome on Android checks
  //     the manifest itself when the app is launched, at most about once a
  //     day, and rebuilds the WebAPK if they changed. The hub helps by serving
  //     the manifest no-cache and the icons under content-hashed URLs, so that
  //     check sees a change as soon as it runs.
  async function refreshWorker() {
    const sw = navigator.serviceWorker;
    if (!sw?.getRegistration) return;
    try {
      const registration = await within(sw.getRegistration(), WORKER_WAIT_MS);
      if (registration?.update) await within(registration.update(), WORKER_WAIT_MS);
    } catch { /* getRegistration threw: the current worker stays in place */ }
  }
  // Guards the wait for the service worker, so a second tap on Reload during
  // it does not start a second round.
  let reloading = false;
  function refuse(message) { toast(message, 6000); return false; }
  async function reloadApp() {
    if (reloading) return true;
    const before = reloadBlocker();
    if (before) return refuse(before);
    reloading = true;
    try {
      if ((await fetchBuild(REACH_TIMEOUT_MS)) === null) {
        return refuse("Can't reach the hub right now. Reload when you're back online.");
      }
      Trio.ui?.setLive?.('Reloading');
      await refreshWorker();
      // The waits above take up to a few seconds; an image attached or a
      // dictation started meanwhile counts as much as one from before.
      const after = reloadBlocker();
      if (after) return refuse(after);
      saveDrafts();
      window.location.reload();
      return true;
    } finally { reloading = false; }
  }

  // ── Build version check ───────────────────────────────────────────────
  // nth_web stamps the build in <meta name="nth-build"> and serves the same value
  // at /api/version. When they differ, the hub has been updated since this
  // page loaded. The check is a ~50-byte GET, made on a slow timer, when the
  // app comes back to the foreground, and when a live stream reconnects
  // (a restarted hub is the usual reason for both).
  const CHECK_EVERY_MS = 10 * 60 * 1000;
  const MIN_GAP_MS = 30 * 1000;
  const loadedBuild = () => {
    try { return document.querySelector('meta[name="nth-build"]')?.getAttribute('content') || ''; }
    catch { return ''; }
  };
  let servedBuild = '';
  let lastCheck = 0;
  let dismissedBuild = '';
  function updateAvailable() {
    return !!(servedBuild && loadedBuild() && servedBuild !== loadedBuild());
  }
  async function checkForUpdate() {
    lastCheck = Date.now();
    const build = await fetchBuild(CHECK_TIMEOUT_MS);
    if (!build) return false;   // offline: the connection pill already says so
    servedBuild = build;
    renderPill();
    return updateAvailable();
  }
  function maybeCheck() {
    if (document.hidden) return;
    if (Date.now() - lastCheck < MIN_GAP_MS) return;
    checkForUpdate();
  }

  function renderPill() {
    let pill = document.getElementById('update-pill');
    const show = updateAvailable() && servedBuild !== dismissedBuild;
    if (!show) { if (pill) pill.hidden = true; return; }
    if (!pill || !pill.querySelector('.update-pill-reload')) {
      pill = pill || document.createElement('div');
      pill.id = 'update-pill';
      pill.className = 'update-pill';
      pill.setAttribute('role', 'status');
      pill.innerHTML = '<span class="update-pill-text">Update available</span>'
        + '<button type="button" class="update-pill-reload">Reload</button>'
        + '<button type="button" class="update-pill-dismiss" aria-label="Dismiss update notice" title="Not now">'
        + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg></button>';
      pill.querySelector('.update-pill-reload').addEventListener('click', () => reloadApp());
      pill.querySelector('.update-pill-dismiss').addEventListener('click', () => {
        dismissedBuild = servedBuild;
        renderPill();
      });
      // Inside #app, so the .app.nav-open rule in 10-shell.css can hide it
      // while the nav drawer is open.
      if (!pill.parentNode) (document.getElementById('app') || document.body).appendChild(pill);
    }
    pill.hidden = false;
  }

  // ── Pull to refresh ───────────────────────────────────────────────────
  // Where a pull may start, and why:
  //   * the top bar. It never scrolls and holds no text to select, so a
  //     downward drag there has no other meaning. It is on screen in every
  //     view, including a long conversation scrolled to its newest message,
  //     which is where a chat is nearly always read;
  //   * the message list or a workspace page, only when it, and anything
  //     scrollable between it and the finger, is already at its top when the
  //     finger lands, as with Chrome's own gesture. A scroll that reaches the
  //     top does not roll on into a refresh. A list that is still
  //     loading marks itself aria-busy and is skipped, so a future "load older
  //     messages" at the top can never be mistaken for a pull.
  // Never inside the composer, drawers, menus, dialogs or form fields; never
  // with the nav drawer, a dialog or a text selection open; never with two
  // fingers. A finger that takes longer than holdMs from touchdown to leaving
  // the dead zone was resting, which makes it a long press (time copy,
  // message actions, text selection), not a pull.
  const PULL = {
    deadZone: 10,       // px of finger travel before a drag counts as a pull
    damping: 0.5,       // the indicator moves half as far as the finger
    threshold: 64,      // damped px; about 140px of finger travel
    max: 112,           // damped px the indicator can travel
    holdMs: 450,        // touchdown to past the dead zone; slower is a long press
  };
  const BLOCKED = '.composer-shell, .sidebar, .channel-drawer, .agent-drawer, dialog, .channel-menu, '
    + '.message-actions-menu, .lightbox, .push-onboard, .update-pill, input, textarea, select, .msg-time';
  const SCROLLERS = '.messages, .workspace-view';
  function matches(query) {
    try { return !!window.matchMedia?.(query)?.matches; } catch { return false; }
  }
  function installed() {
    if (navigator.standalone === true) return true;
    return ['standalone', 'fullscreen', 'minimal-ui'].some(mode => matches(`(display-mode: ${mode})`));
  }
  function touchDevice() {
    return (navigator.maxTouchPoints || 0) > 0 || matches('(pointer: coarse)');
  }
  function pullEnabled() { return installed() && touchDevice(); }
  function hasSelection() {
    try { return !!String(window.getSelection?.() || '').length; } catch { return false; }
  }
  function overlayOpen() {
    const app = document.getElementById('app');
    if (app?.classList.contains('nav-open') || app?.classList.contains('channel-details-open')) return true;
    if (state.activeMessageActions) return true;
    try { return !!document.querySelector('dialog[open]'); } catch { return false; }
  }
  // 'header', 'list', or null when a pull may not start from this target.
  function surfaceFor(target) {
    if (!target?.closest || target.closest(BLOCKED)) return null;
    if (target.closest('[contenteditable="true"]')) return null;
    if (overlayOpen() || hasSelection()) return null;
    if (target.closest('.conversation-header')) return 'header';
    const scroller = target.closest(SCROLLERS);
    if (!scroller || scrolledDown(target, scroller)) return null;
    if (scroller.getAttribute('aria-busy') === 'true') return null;
    return 'list';
  }
  // Whether the target, the scroller or anything between them is scrolled
  // away from its top. A pull inside a scrolled code block is a scroll.
  function scrolledDown(target, scroller) {
    for (let node = target; node; node = node.parentElement) {
      if ((node.scrollTop || 0) > 0) return true;
      if (node === scroller) break;
    }
    return false;
  }

  let gesture = null;
  // After a pull, the element it started on may still get the click some
  // browsers fire on release. Only that element, only briefly, and only until
  // the next touch begins.
  let suppressClick = null;
  const firstTouch = event => event.touches?.[0] || event.changedTouches?.[0] || null;
  function indicator() {
    let node = document.getElementById('pull-refresh');
    if (node && node.querySelector('.pull-refresh-label')) return node;
    node = node || document.createElement('div');
    node.id = 'pull-refresh';
    node.className = 'pull-refresh';
    node.setAttribute('aria-hidden', 'true');
    node.innerHTML = '<svg class="pull-refresh-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M6 13l6 6 6-6"/></svg>'
      + '<span class="pull-refresh-label">Pull to refresh</span>';
    node.hidden = true;
    if (!node.parentNode) document.body.appendChild(node);
    return node;
  }
  function paint(distance, phase) {
    const node = indicator();
    const ready = phase === 'refreshing' || distance >= PULL.threshold;
    node.hidden = false;
    node.classList.toggle('ready', ready);
    node.classList.toggle('refreshing', phase === 'refreshing');
    node.querySelector('.pull-refresh-label').textContent =
      phase === 'refreshing' ? 'Refreshing…' : ready ? 'Release to refresh' : 'Pull to refresh';
    node.style.setProperty('--pull', Math.round(distance) + 'px');
  }
  function hide() {
    const node = document.getElementById('pull-refresh');
    if (!node) return;
    node.hidden = true;
    node.classList.remove('ready', 'refreshing');
    node.style.setProperty('--pull', '0px');
  }

  function start(event) {
    // A second finger landing mid-pull arrives here too: hide what it drew.
    cancel();
    suppressClick = null;
    if (!pullEnabled()) return false;
    if ((event.touches?.length || 1) !== 1) return false;
    const surface = surfaceFor(event.target);
    const touch = firstTouch(event);
    if (!surface || !touch) return false;
    gesture = {
      surface, phase: 'armed', distance: 0,
      x: touch.clientX, y: touch.clientY, at: Date.now(), target: event.target,
      scroller: surface === 'list' ? event.target.closest(SCROLLERS) : null,
    };
    return true;
  }
  function cancel() {
    if (gesture?.phase === 'pulling') hide();
    gesture = null;
  }
  function move(event) {
    if (!gesture) return null;
    if ((event.touches?.length || 1) !== 1) { cancel(); return null; }
    const touch = firstTouch(event);
    if (!touch) return gesture.phase;
    const dx = touch.clientX - gesture.x;
    const dy = touch.clientY - gesture.y;
    if (gesture.phase === 'armed') {
      if (Math.abs(dx) > PULL.deadZone && Math.abs(dx) > Math.abs(dy)) { cancel(); return null; }
      if (dy < -PULL.deadZone) { cancel(); return null; }
      if (dy <= PULL.deadZone) return 'armed';
      if (Date.now() - gesture.at > PULL.holdMs) { cancel(); return null; }
      if (gesture.scroller && scrolledDown(gesture.target, gesture.scroller)) { cancel(); return null; }
      if (hasSelection()) { cancel(); return null; }
      gesture.phase = 'pulling';
    }
    if (dy <= 0) { cancel(); return null; }
    gesture.distance = Math.min(PULL.max, Math.max(0, (dy - PULL.deadZone) * PULL.damping));
    paint(gesture.distance, 'pulling');
    return 'pulling';
  }
  function end() {
    const done = gesture;
    gesture = null;
    if (!done || done.phase !== 'pulling') return false;
    suppressClick = { target: done.target, until: Date.now() + 600 };
    if (done.distance < PULL.threshold) { hide(); return false; }
    paint(PULL.max, 'refreshing');
    reloadApp().then(ok => { if (!ok) hide(); }, hide);
    return true;
  }
  function swallowClick(event) {
    const guard = suppressClick;
    if (!guard) return false;
    suppressClick = null;
    if (Date.now() > guard.until || !guard.target?.contains?.(event.target)) return false;
    event.preventDefault?.();
    event.stopPropagation?.();
    return true;
  }

  // ── Mount ─────────────────────────────────────────────────────────────
  function mount(ctx) {
    // Passive everywhere: the gesture never calls preventDefault, so it costs
    // scrolling nothing. The document never scrolls, so there is no native
    // gesture underneath to fight.
    const passive = { passive: true };
    const listeners = [
      [document, 'touchstart', start, passive],
      [document, 'touchmove', move, passive],
      [document, 'touchend', end, passive],
      [document, 'touchcancel', cancel, passive],
      [document, 'click', swallowClick, true],
      [document, 'visibilitychange', maybeCheck],
      [window, 'online', maybeCheck],
      [window, 'pagehide', saveDrafts],
      [window, 'pageshow', onPageShow],
    ];
    listeners.forEach(([target, type, fn, opts]) => target.addEventListener?.(type, fn, opts));
    const onConnection = event => {
      const now = event?.detail?.state;
      if (now === 'connected' || now === 'workspace:connected') maybeCheck();
    };
    Trio.events?.addEventListener?.('connection', onConnection);
    const timer = setInterval(maybeCheck, CHECK_EVERY_MS);
    ctx?.onUnmount?.(() => {
      listeners.forEach(([target, type, fn, opts]) => target.removeEventListener?.(type, fn, opts));
      Trio.events?.removeEventListener?.('connection', onConnection);
      clearInterval(timer);
      cancel();
    });
  }

  Trio.appRefresh = {
    mount, reloadApp, checkForUpdate, updateAvailable, loadedBuild,
    saveDrafts, restoreDrafts, onPageShow, conversationKey, unsentImages, installed, pullEnabled,
    pull: { start, move, end, cancel, surfaceFor, swallowClick, current: () => gesture },
    PULL, DRAFTS_KEY, WORKER_WAIT_MS, MIN_GAP_MS,
  };
})();
