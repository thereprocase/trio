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

  // The hub deletes a subscription when the push service keeps refusing it or
  // reports it gone, when a guest's row sits idle for a month, and nothing
  // tells the phone. The page therefore remembers, per channel, that this
  // device was subscribed and with which mode, so it can notice the row has
  // gone and offer to restore it. The wording names no cause, because the
  // page cannot tell which one it was.
  const dropped = channel => `This device no longer gets notifications for #${channel}. Turn them back on?`;
  const REMEMBER_PREFIX = 'nth.push.subscribed.';
  function remembered(channel) {
    try { return localStorage.getItem(REMEMBER_PREFIX + channel) || ''; } catch { return ''; }
  }
  function remember(channel, mode) {
    try {
      if (mode && mode !== 'off') localStorage.setItem(REMEMBER_PREFIX + channel, mode);
      else localStorage.removeItem(REMEMBER_PREFIX + channel);
    } catch { /* private mode: the notice is a convenience */ }
  }
  // Whether to offer "Turn them back on": this device was subscribed here, the
  // hub no longer has the row, and nothing else explains why (an unnamed
  // visitor has no rows to show; a blocker already says what is wrong).
  function droppedNotice({ rememberedMode, subscribedHere, named, blocked }) {
    return !!rememberedMode && rememberedMode !== 'off' && !subscribedHere && named !== false && !blocked;
  }

  // "14:05" today, "14:05, Mar 3" on another day this year, with the year
  // otherwise; "never" before the first delivery.
  function lastDelivered(seconds, now = new Date()) {
    if (!seconds) return 'never';
    const when = new Date(seconds * 1000);
    const pad = n => String(n).padStart(2, '0');
    const time = pad(when.getHours()) + ':' + pad(when.getMinutes());
    if (when.toDateString() === now.toDateString()) return time;
    const opts = { month: 'short', day: 'numeric' };
    if (when.getFullYear() !== now.getFullYear()) opts.year = 'numeric';
    return time + ', ' + when.toLocaleDateString(undefined, opts);
  }

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
      + `<div class="push-dropped" hidden><p>${esc(dropped(channel))}</p><button type="button" class="btn sm push-resubscribe">Turn back on</button> <button type="button" class="btn sm push-dismiss">Not now</button></div>`
      + `<div class="push-modes" role="group" aria-label="Phone notifications for #${esc(channel)}">${buttons}</div>`
      + '<label class="push-option"><input type="checkbox" class="push-show-text"><span>Show message text on the lock screen</span></label>'
      + '<div class="push-device" hidden><span class="push-last"></span><button type="button" class="btn sm push-test">Send test</button></div>'
      + '<p class="push-status" role="status" aria-live="polite"></p>';
    section.querySelectorAll('.push-mode').forEach(btn => {
      btn.addEventListener('click', () => choose(section, channel, btn.getAttribute('data-push-mode')));
    });
    section.querySelector('.push-resubscribe')?.addEventListener('click', () => choose(section, channel, remembered(channel) || 'all', { fresh: true }));
    section.querySelector('.push-dismiss')?.addEventListener('click', () => { remember(channel, 'off'); showDropped(section, false); });
    section.querySelector('.push-show-text')?.addEventListener('change', () => toggleText(section, channel));
    section.querySelector('.push-test')?.addEventListener('click', () => sendTest(section, channel));
    showDropped(section, false);
    textKnown.set(section, false);
    showDevice(section, null);
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
    section.querySelectorAll('.push-mode, .push-show-text, .push-test, .push-resubscribe')
      .forEach(el => { el.disabled = disabled; });
  }
  function showDropped(section, on) {
    const el = section.querySelector('.push-dropped');
    if (el) el.hidden = !on;
  }
  // This device's row on this channel (null when not subscribed): the text
  // choice it carries, and when the push service last accepted a notification
  // for it here. Without a row the checkbox still works, and its value goes
  // with the next subscribe.
  const deviceRows = new WeakMap();
  // Whether the checkbox reflects something real: a row the hub reported, or
  // the person's own tick. Until then a subscribe leaves show_text out, so a
  // tap made before the status loads (or after it failed) cannot overwrite a
  // stored "show" with the unticked default.
  const textKnown = new WeakMap();
  function showDevice(section, row) {
    deviceRows.set(section, row);
    const box = section.querySelector('.push-show-text');
    if (box && row) { box.checked = !!row.show_text; textKnown.set(section, true); }
    const device = section.querySelector('.push-device');
    if (device) device.hidden = !row;
    const last = section.querySelector('.push-last');
    if (last) last.textContent = 'Last delivered: ' + lastDelivered(row?.last_ok_at);
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
    if (why) { showMode(section, 'off'); showDevice(section, null); return; }
    let sub = null;
    try {
      if (supported() && Notification.permission === 'granted') sub = await browserSubscription(false);
    } catch { /* no registration yet: this device is not subscribed */ }
    if (section.dataset.channel !== channel) return;
    const endpoint = sub?.endpoint || '';
    const mine = endpoint ? (status?.subscriptions || []).find(s => s.endpoint === endpoint) : null;
    showMode(section, mine ? mine.mode : 'off');
    showDevice(section, mine || null);
    if (status) {
      // Devices subscribed before this build start being remembered here.
      if (mine) remember(channel, mine.mode);
      showDropped(section, droppedNotice({
        rememberedMode: remembered(channel), subscribedHere: !!mine, named: status.named, blocked: !!why,
      }));
    }
    // Quietly renew this device's row with the mode it already has: no
    // prompt, no visible change. It keeps the row's tier current with this
    // identity (rows from older builds are re-tiered) and marks it in use, so
    // an active guest is never aged out.
    if (sub && mine && status?.named) {
      api.post('/api/push/subscribe', { subscription: sub.toJSON(), channel, mode: mine.mode }, false)
        .catch(() => { /* best effort; the visible state is already right */ });
    }
  }

  // `fresh` replaces the browser subscription even without a conflict: the
  // "Turn back on" offer uses it, because the endpoint the hub dropped is the
  // one the push service kept refusing.
  async function choose(section, channel, mode, { fresh = false } = {}) {
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
        remember(channel, 'off');
        showDropped(section, false);
        showMode(section, 'off');
        showDevice(section, null);
        return;
      }
      if ((await permission) !== 'granted') {
        setStatus(section, 'Notifications were not allowed, so nothing changed.');
        return;
      }
      const box = section.querySelector('.push-show-text');
      const body = s => ({ subscription: s.toJSON(), channel, mode,
                           ...(box && textKnown.get(section) ? { show_text: !!box.checked } : {}) });
      let result = null;
      let note = '';
      // Nothing to replace when the browser no longer holds a subscription.
      let replace = fresh && !!(await browserSubscription(false).catch(() => null));
      let sub = await browserSubscription(true);
      if (!replace) {
        try {
          result = await api.post('/api/push/subscribe', body(sub), false);
        } catch (e) {
          // 409: this browser's endpoint already belongs to another identity
          // (the cookie changed). Replace the browser subscription so this
          // identity gets an endpoint of its own; the old rows then expire at
          // the push service and the hub prunes them.
          if (e?.status !== 409) throw e;
          replace = true;
        }
      }
      if (replace) {
        const oldEndpoint = sub.endpoint;
        await sub.unsubscribe();
        sub = await browserSubscription(true);
        result = await api.post('/api/push/subscribe', body(sub), false);
        note = await moveOtherChannels(channel, oldEndpoint, sub);
      }
      remember(channel, mode);
      showDropped(section, false);
      showMode(section, mode);
      // The hub answers with the stored choice, which is what an omitted
      // show_text kept.
      const showText = typeof result?.show_text === 'boolean' ? result.show_text : !!box?.checked;
      showDevice(section, { mode, show_text: showText, last_ok_at: replace ? null : deviceRows.get(section)?.last_ok_at || null });
      if (note) setStatus(section, section.querySelector('.push-status').textContent + ' ' + note);
    } catch (e) {
      setStatus(section, e?.message || 'Could not change phone notifications.');
    } finally {
      disable(section, false);
    }
  }

  // The checkbox saves at once for a subscribed device. Otherwise it is only
  // a choice on the page, sent with the next subscribe.
  async function toggleText(section, channel) {
    const box = section.querySelector('.push-show-text');
    if (!box) return;
    const want = !!box.checked;
    textKnown.set(section, true);
    const row = deviceRows.get(section);
    if (!row) {
      setStatus(section, want ? 'Message text will show once notifications are on for this device.'
                              : 'Message text will stay hidden.');
      return;
    }
    box.disabled = true;
    try {
      const sub = await browserSubscription(false);
      if (!sub) throw new Error('This device has no push subscription. Pick a mode to turn notifications on.');
      await api.post('/api/push/settings', { endpoint: sub.endpoint, channel, show_text: want }, false);
      showDevice(section, { ...row, show_text: want });
      setStatus(section, want ? 'Notifications on this device will show message text.'
                              : 'Notifications on this device will hide message text.');
    } catch (e) {
      box.checked = !want;
      setStatus(section, e?.message || 'Could not change that setting.');
    } finally {
      box.disabled = false;
    }
  }

  async function sendTest(section, channel) {
    const button = section.querySelector('.push-test');
    if (button) button.disabled = true;
    try {
      const sub = await browserSubscription(false);
      if (!sub) throw new Error('This device has no push subscription. Pick a mode to turn notifications on.');
      const result = await api.post('/api/push/test', { endpoint: sub.endpoint, channel }, false);
      const row = deviceRows.get(section);
      if (row) showDevice(section, { ...row, last_ok_at: result?.last_ok_at || Date.now() / 1000 });
      setStatus(section, 'Test sent. It should appear on this device within a few seconds.');
    } catch (e) {
      setStatus(section, e?.message || 'Could not send a test notification.');
      // 410: the hub has just forgotten this endpoint; show the restore offer.
      if (e?.status === 410) refresh(section, channel);
    } finally {
      if (button) button.disabled = false;
    }
  }

  // After a 409 the browser endpoint was replaced, and this identity's other
  // channels still name the old one, which the push service now rejects. The
  // server moves all of them to the new endpoint in one transaction (so the
  // quota never costs a channel); report which channels came along.
  async function moveOtherChannels(channel, oldEndpoint, sub) {
    let result;
    try { result = await api.post('/api/push/move', { old_endpoint: oldEndpoint, subscription: sub.toJSON() }, false); }
    catch { return 'Phone notifications on your other channels may now be off — check them there.'; }
    const moved = (result?.moved || []).filter(c => c !== channel).map(c => '#' + c);
    return moved.length ? 'Also kept on for ' + moved.join(', ') + '.' : '';
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

  Trio.push = { mount, render, MODES, blocker, isIos, isStandalone, supported, keyBytes, droppedNotice, lastDelivered };
})();
