(() => {
  'use strict';
  const Trio = window.Trio;
  if (!Trio) throw new Error('Time formatting requires the Trio namespace');

  // Message times are read against bus traces and logs, so every surface shows
  // seconds and carries the exact UTC instant (ISO 8601, milliseconds, Z) in a
  // <time datetime> attribute and tooltip. Tapping a time copies that instant.
  //
  // The server writes Python isoformat(): six fractional digits and a +00:00
  // offset ("2026-10-08T14:25:12.262123+00:00"). Some engines reject more than
  // three fractional digits, so the string is normalised before it reaches
  // Date. Anything that does not look like an ISO instant is left alone and
  // shown verbatim, so a malformed value can never render as "Invalid Date".
  const ISO_RE = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(:\d{2})?(?:[.,](\d+))?\s*(Z|[+-]\d{2}(?::?\d{2})?)?$/i;

  function parse(value) {
    if (value instanceof Date) return Number.isFinite(value.getTime()) ? value : null;
    if (typeof value === 'number') {
      const d = new Date(value);
      return Number.isFinite(d.getTime()) ? d : null;
    }
    if (typeof value !== 'string') return null;
    const m = ISO_RE.exec(value.trim());
    if (!m) return null;
    const fraction = m[4] ? '.' + (m[4] + '00').slice(0, 3) : '';
    // A zoneless timestamp is read as UTC, the zone the hub writes.
    let zone = (m[5] || 'Z').toUpperCase();
    if (zone !== 'Z') {
      const digits = zone.slice(1).replace(':', '');
      zone = zone[0] + digits.slice(0, 2) + ':' + (digits.slice(2) || '00');
    }
    const d = new Date(m[1] + 'T' + m[2] + (m[3] || ':00') + fraction + zone);
    return Number.isFinite(d.getTime()) ? d : null;
  }

  function mode() {
    const prefs = Trio.preferences?.read?.() || Trio.state?.preferences || {};
    return prefs.messageTimes === 'utc' ? 'utc' : 'local';
  }
  const pad = n => String(n).padStart(2, '0');

  function iso(value) {
    const d = parse(value);
    return d ? d.toISOString() : '';
  }
  // HH:MM:SS (24-hour) in the browser's zone, or HH:MM:SSZ in UTC.
  function clock(value, as = mode()) {
    if (value == null || value === '') return '';
    const d = parse(value);
    if (!d) return String(value);
    if (as === 'utc') return pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()) + ':' + pad(d.getUTCSeconds()) + 'Z';
    return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }
  // Calendar day in the same zone the clock uses, so a day separator and the
  // times under it always agree about which side of midnight a message is on.
  function dayKey(value, as = mode()) {
    const d = parse(value);
    if (!d) return '';
    return as === 'utc'
      ? d.getUTCFullYear() + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate())
      : d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }
  // One formatter per zone: building an Intl formatter per call cost ~30 ms
  // per live insert once day separators were derived for every card.
  const dayFormats = {};
  function dayFormat(as) {
    if (!dayFormats[as]) {
      const options = { weekday: 'short', month: 'short', day: 'numeric' };
      if (as === 'utc') options.timeZone = 'UTC';
      dayFormats[as] = new Intl.DateTimeFormat([], options);
    }
    return dayFormats[as];
  }
  // Day label for a separator, which stands alone and so names its zone in
  // UTC mode ("Thu, Oct 8 UTC").
  function day(value, as = mode()) {
    const d = parse(value);
    if (!d) return '';
    const text = dayFormat(as).format(d);
    return as === 'utc' ? text + ' UTC' : text;
  }
  // `withDay`: true always prefixes the day; 'auto' prefixes it only when the
  // instant is not today, for list views that span many days. The clock's
  // trailing Z is the single zone marker, so the day carries none.
  function label(value, opts = {}) {
    const as = opts.mode || mode();
    const time = clock(value, as);
    const d = parse(value);
    if (!d || !opts.withDay) return time;
    if (opts.withDay === 'auto' && dayKey(d, as) === dayKey(opts.now || new Date(), as)) return time;
    return dayFormat(as).format(d) + ' ' + time;
  }
  // Full date and time with one zone marker: "Thu, Oct 8 10:25:12" locally,
  // "Thu, Oct 8 14:25:12Z" in UTC. For any label that needs both halves (an
  // expiry, a deadline) instead of joining day() and clock() by hand.
  function dateTime(value, opts = {}) { return label(value, { ...opts, withDay: true }); }

  // A <time> node for DOM-built views. `prefix` is an optional node placed
  // before the clock text (the conversation's "#id · " marker).
  function element(value, opts = {}) {
    const node = document.createElement('time');
    node.className = 'msg-time';
    if (opts.prefix) node.append(opts.prefix);
    node.append(document.createTextNode(label(value, opts)));
    const instant = iso(value);
    if (instant) {
      node.setAttribute('datetime', instant);
      node.setAttribute('title', instant);
      if (opts.tabbable !== false) node.setAttribute('tabindex', '0');
    }
    return node;
  }
  // The same node as markup, for views rendered through innerHTML. A time
  // nested in a button must not be a separate tab stop (`tabbable: false`).
  function html(value, opts = {}) {
    const text = label(value, opts);
    if (!text) return '';
    const esc = Trio.markdown?.escapeHtml || (s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
    const instant = iso(value);
    if (!instant) return `<time class="msg-time">${esc(text)}</time>`;
    const tab = opts.tabbable === false ? '' : ' tabindex="0"';
    return `<time class="msg-time" datetime="${instant}" title="${instant}"${tab}>${esc(text)}</time>`;
  }

  // ── Copy on tap ─────────────────────────────────────────────────────────
  // Delegated at the document in the capture phase so it works for every
  // surface (including innerHTML-built cards) and runs before a containing
  // card's own click handler, which would otherwise navigate away.
  const LONG_PRESS_MS = 500;
  const REPEAT_GUARD_MS = 800;
  let lastPointerType = '';
  let pressTimer = null;
  let pressStart = { x: 0, y: 0 };
  // A long press only ARMS the copy. Clipboard writes need user activation,
  // which a timer callback does not have (Firefox, iOS, and the execCommand
  // fallback on plain http all refuse it); the release that ends the press is
  // an activating gesture, so the copy runs there.
  let armed = null;
  // The node the release just copied, so the click some browsers fire after
  // it does not copy and announce a second time.
  let swallowClick = null;
  let lastCopy = { node: null, at: 0 };

  function target(event) {
    const node = event?.target?.closest?.('.msg-time');
    return node && node.getAttribute?.('datetime') ? node : null;
  }
  function copyInstant(node) {
    const instant = node.getAttribute('datetime');
    const now = Date.now();
    // A long press can be followed by a click or contextmenu for the same
    // gesture; copy and announce once.
    if (lastCopy.node === node && now - lastCopy.at < REPEAT_GUARD_MS) return Promise.resolve(false);
    lastCopy = { node, at: now };
    const ui = Trio.ui || {};
    const copy = ui.copyText ? ui.copyText(instant) : Promise.reject(new Error('clipboard unavailable'));
    return Promise.resolve(copy).then(() => {
      node.classList.add('copied');
      setTimeout(() => node.classList.remove('copied'), 1200);
      ui.toast?.('Copied ' + instant, 1800);
      ui.setLive?.('Copied ' + instant);
      return true;
    }).catch(() => {
      ui.toast?.('Could not copy to the clipboard. The time is ' + instant + ' — select it here to copy it by hand.', 8000);
      ui.setLive?.('Could not copy. The time is ' + instant);
      return false;
    });
  }
  function clearPress() { if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; } }
  function onPointerDown(event) {
    lastPointerType = event.pointerType || '';
    swallowClick = null;
    armed = null;
    clearPress();
    const node = target(event);
    if (!node || event.pointerType !== 'touch') return;
    pressStart = { x: event.clientX || 0, y: event.clientY || 0 };
    pressTimer = setTimeout(() => { pressTimer = null; armed = node; }, LONG_PRESS_MS);
  }
  function onRelease(event) {
    clearPress();
    if (!armed) return;
    const node = armed;
    armed = null;
    event.preventDefault?.();
    copyInstant(node);
    swallowClick = node;
  }
  function onActivate(event) {
    const node = target(event);
    if (!node) return;
    if (event.type === 'keydown' && event.key !== 'Enter' && event.key !== ' ') return;
    // Right-click with a mouse keeps the browser menu. A touch long-press
    // surfaces as contextmenu on Android; it suppresses the native menu and
    // arms the copy for the release, like the long-press timer does.
    if (event.type === 'contextmenu' && lastPointerType !== 'touch') return;
    event.preventDefault?.();
    event.stopPropagation?.();
    clearPress();
    if (event.type === 'contextmenu') { armed = node; return; }
    if (event.type === 'click' && swallowClick === node) { swallowClick = null; return; }
    copyInstant(node);
  }
  if (!Trio.time && typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('click', onActivate, true);
    document.addEventListener('keydown', onActivate, true);
    document.addEventListener('contextmenu', onActivate, true);
    document.addEventListener('pointerdown', onPointerDown, true);
    // pointerup and touchend both end a touch; whichever arrives first copies.
    document.addEventListener('pointerup', onRelease, true);
    document.addEventListener('touchend', onRelease, true);
    // A cancel after the press has armed is the browser taking over the
    // gesture; touchend still follows, so only the pending timer is dropped.
    document.addEventListener('pointercancel', clearPress, true);
    document.addEventListener('pointermove', event => {
      // Small finger jitter must not cancel a long press; a scroll will.
      if (Math.hypot((event.clientX || 0) - pressStart.x, (event.clientY || 0) - pressStart.y) < 10) return;
      clearPress();
      armed = null;
    }, true);
  }

  Trio.time = { parse, iso, clock, day, dayKey, label, dateTime, element, html, mode, copyInstant };
})();
