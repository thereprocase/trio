(() => {
  'use strict';
  const Trio = window.Trio;
  if (!Trio) throw new Error('Push requires Trio core');
  const { api } = Trio;

  // Phone notifications: Web Push through the service worker at /sw.js.
  // The page asks the browser for a push subscription and hands it to the hub
  // with a mode; the hub encrypts each notification to this browser's keys, so
  // the push service in between relays ciphertext only.
  //
  // Four modes, one per channel per device. Labels are what the operator reads;
  // the ids are the server contract (nth_webpush.PUSH_MODES).
  const MODES = [
    ['all', 'Every message'],
    ['mentions', 'Mentions'],
    ['every5m', 'Every 5 min'],
    ['off', 'Off'],
  ];
  const MODE_HELP = {
    all: 'A notification for every new message in this channel.',
    mentions: 'Only when someone @-mentions you or @all, or DMs you.',
    every5m: 'At most one summary every five minutes.',
    off: 'No phone notifications for this channel.',
  };

  // Shown when the server reports that no hub process is sending pushes: a
  // single-channel viewer, or a dashboard started with --no-agent-control.
  const NOT_DELIVERING = 'Your choice is saved, but no hub is sending phone notifications right now. They are sent by the hub\'s main dashboard (nth_web.py started without a channel).';

  function supported() {
    return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
  }
  // iPadOS reports itself as a Mac; touch points tell them apart.
  function isIos() {
    const ua = navigator.userAgent || '';
    return /iPad|iPhone|iPod/.test(ua) || (/Macintosh/.test(ua) && (navigator.maxTouchPoints || 0) > 1);
  }
  function isStandalone() {
    if (navigator.standalone === true) return true;
    try { return !!window.matchMedia?.('(display-mode: standalone)')?.matches; } catch { return false; }
  }

  function keyBytes(b64url) {
    const pad = '='.repeat((4 - (b64url.length % 4)) % 4);
    const raw = atob((b64url + pad).replace(/-/g, '+').replace(/_/g, '/'));
    const out = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }
  function sameKey(a, b) {
    if (!a || !b) return false;
    const x = new Uint8Array(a), y = new Uint8Array(b);
    return x.length === y.length && x.every((v, i) => v === y[i]);
  }

  let registration = null;
  async function ensureRegistration() {
    if (registration) return registration;
    await navigator.serviceWorker.register('/sw.js', { scope: '/' });
    registration = await navigator.serviceWorker.ready;
    return registration;
  }

  let publicKey = null;
  async function vapidKey() {
    if (publicKey) return publicKey;
    const data = await api.get('/api/push/vapid-public-key', false);
    publicKey = data.publicKey;
    return publicKey;
  }

  // The browser's subscription, created if needed. If the hub's key changed
  // (a restored or rebuilt hub), the old subscription can never be delivered
  // to, so it is replaced rather than reused.
  async function browserSubscription(create) {
    const reg = await ensureRegistration();
    let sub = await reg.pushManager.getSubscription();
    if (!create) return sub;
    const key = keyBytes(await vapidKey());
    if (sub && !sameKey(sub.options?.applicationServerKey, key)) {
      try { await sub.unsubscribe(); } catch { /* replaced below either way */ }
      sub = null;
    }
    return sub || reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
  }

  function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

  // ── Channel-drawer control ───────────────────────────────────────────
  // Selectors are classes rather than attribute selectors so the Node DOM
  // harness (tests/dom-harness.js) can drive this exactly as shipped.
  // Rendered into the placeholder section 20-workspace puts in the channel
  // details drawer. The buttons are real buttons rather than a <select>: iOS
  // grants the notification prompt only inside a user gesture, and a picker's
  // change event can arrive after that gesture has expired.
  function render(section, channel) {
    if (!section || !channel) return;
    section.dataset.channel = channel;
    const buttons = MODES.map(([id, label]) =>
      `<button type="button" class="btn sm push-mode" data-push-mode="${id}" aria-pressed="false">${esc(label)}</button>`).join('');
    section.innerHTML = '<h3>Phone notifications</h3>'
      + '<p class="push-hint" hidden></p>'
      + `<div class="push-modes" role="group" aria-label="Phone notifications for #${esc(channel)}">${buttons}</div>`
      + '<p class="push-status" role="status" aria-live="polite"></p>';
    section.querySelectorAll('.push-mode').forEach(btn => {
      btn.addEventListener('click', () => choose(section, channel, btn.getAttribute('data-push-mode')));
    });
    refresh(section, channel);
  }

  function setStatus(section, text) {
    const el = section.querySelector('.push-status');
    if (el) el.textContent = text;
  }
  function setHint(section, text) {
    const el = section.querySelector('.push-hint');
    if (!el) return;
    el.textContent = text || '';
    el.hidden = !text;
  }
  function showMode(section, mode) {
    section.querySelectorAll('.push-mode').forEach(btn => {
      const on = btn.getAttribute('data-push-mode') === mode;
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
      btn.classList.toggle('primary', on);
    });
    const label = (MODES.find(([id]) => id === mode) || MODES[3])[1];
    setStatus(section, `This device: ${label}. ${MODE_HELP[mode] || ''}`);
  }
  function disable(section, disabled) {
    section.querySelectorAll('.push-mode').forEach(btn => { btn.disabled = disabled; });
  }

  // Why this device cannot subscribe, or '' when it can.
  function blocker(secureUrl) {
    if (isIos() && !isStandalone()) {
      return 'On iPhone and iPad, add this page to your Home Screen first (Share → Add to Home Screen), then open it from there. iOS only allows web push from installed apps (iOS 16.4 and later).';
    }
    if (!window.isSecureContext) {
      return 'Phone notifications need the https address' + (secureUrl ? ': ' + secureUrl : ' (the hub\'s MagicDNS name, served with --tailscale-tls).');
    }
    if (!supported()) return 'This browser does not support web push.';
    if (Notification.permission === 'denied') {
      return 'Notifications are blocked for this site. Allow them in the browser\'s site settings, then try again.';
    }
    return '';
  }

  async function refresh(section, channel) {
    let status = null;
    try { status = await api.get('/api/push/status?channel=' + encodeURIComponent(channel), false); }
    catch (e) { setStatus(section, e.message || 'Could not load notification settings.'); }
    if (section.dataset.channel !== channel) return;      // drawer moved on
    if (status && status.enabled === false) {
      setHint(section, 'This hub cannot send phone notifications (its Python lacks the cryptography package).');
      disable(section, true);
      return;
    }
    const why = blocker(status?.secure_url || '');
    setHint(section, why || (status && status.delivering === false ? NOT_DELIVERING : ''));
    disable(section, !!why);
    if (why) { showMode(section, 'off'); return; }
    let endpoint = '';
    try {
      if (supported() && Notification.permission === 'granted') {
        endpoint = (await browserSubscription(false))?.endpoint || '';
      }
    } catch { /* no registration yet: this device is not subscribed */ }
    const mine = (status?.subscriptions || []).find(s => s.endpoint === endpoint);
    showMode(section, endpoint && mine ? mine.mode : 'off');
  }

  async function choose(section, channel, mode) {
    // Ask for permission FIRST and synchronously in the tap: iOS refuses the
    // prompt once the handler has awaited anything.
    let permission = Promise.resolve(window.Notification?.permission);
    if (mode !== 'off' && supported() && Notification.permission === 'default') {
      permission = Notification.requestPermission();
    }
    disable(section, true);
    try {
      if (mode === 'off') {
        const sub = supported() ? await browserSubscription(false).catch(() => null) : null;
        // The browser subscription is shared by every channel on this device,
        // so turning one channel off removes only that channel's row.
        if (sub) await api.post('/api/push/unsubscribe', { endpoint: sub.endpoint, channel }, false);
        showMode(section, 'off');
        return;
      }
      if ((await permission) !== 'granted') {
        setStatus(section, 'Notifications were not allowed, so nothing changed.');
        return;
      }
      let sub = await browserSubscription(true);
      try {
        await api.post('/api/push/subscribe', { subscription: sub.toJSON(), channel, mode }, false);
      } catch (e) {
        // 409: this browser's endpoint already belongs to another identity
        // (the cookie changed). Replace the browser subscription so this
        // identity gets an endpoint of its own; the old rows then expire at
        // the push service and the hub prunes them.
        if (e?.status !== 409) throw e;
        const oldEndpoint = sub.endpoint;
        await sub.unsubscribe();
        sub = await browserSubscription(true);
        await api.post('/api/push/subscribe', { subscription: sub.toJSON(), channel, mode }, false);
        const note = await moveOtherChannels(channel, oldEndpoint, sub);
        showMode(section, mode);
        if (note) setStatus(section, section.querySelector('.push-status').textContent + ' ' + note);
        return;
      }
      showMode(section, mode);
    } catch (e) {
      setStatus(section, e?.message || 'Could not change phone notifications.');
    } finally {
      disable(section, false);
    }
  }

  // After a 409 the browser endpoint was replaced, and this identity's other
  // channels still name the old one, which the push service now rejects. Move
  // each to the new endpoint with its mode; name any that could not be moved,
  // since those are now off.
  async function moveOtherChannels(channel, oldEndpoint, sub) {
    let status;
    try { status = await api.get('/api/push/status?channel=' + encodeURIComponent(channel), false); }
    catch { return ''; }
    const stale = (status?.mine || []).filter(s => s.endpoint === oldEndpoint && s.channel !== channel);
    const moved = [], lost = [];
    for (const row of stale) {
      try {
        await api.post('/api/push/subscribe', { subscription: sub.toJSON(), channel: row.channel, mode: row.mode }, false);
        moved.push('#' + row.channel);
      } catch { lost.push('#' + row.channel); }
    }
    if (stale.length) {
      try { await api.post('/api/push/unsubscribe', { endpoint: oldEndpoint }, false); } catch { /* the push service retires it anyway */ }
    }
    const parts = [];
    if (moved.length) parts.push('Also kept on for ' + moved.join(', ') + '.');
    if (lost.length) parts.push('Now off for ' + lost.join(', ') + ' — turn them on again there.');
    return parts.join(' ');
  }

  // ── Service worker wiring ─────────────────────────────────────────────
  // Registered on every load, not only when someone subscribes: Chrome's
  // install prompt and the notification-click hand-off both need it present.
  function onWorkerMessage(event) {
    const data = event.data || {};
    if (data.type === 'nth-open-channel' && data.channel) Trio.workspace?.openChannel?.(data.channel);
  }
  function mount(ctx) {
    if (!('serviceWorker' in navigator)) return;
    navigator.serviceWorker.addEventListener?.('message', onWorkerMessage);
    ctx?.onUnmount?.(() => navigator.serviceWorker.removeEventListener?.('message', onWorkerMessage));
    if (window.isSecureContext) ensureRegistration().catch(() => { /* push stays unavailable; the page works as before */ });
  }

  Trio.push = { mount, render, MODES, blocker, isIos, isStandalone, supported, keyBytes };
})();
