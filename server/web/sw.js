// nth service worker — web push only.
//
// Served from /sw.js with `Service-Worker-Allowed: /` and `Cache-Control:
// no-cache` so its scope is the whole app and an update is picked up on the
// next navigation.
//
// There is deliberately NO fetch handler and NO cache. The dashboard is one
// inlined page that changes with every deploy; a caching worker is how an
// installed app ends up stuck on last week's bundle with no way for the user
// to tell. Every request goes straight to the network, as it would without
// this file.
'use strict';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));

function readPayload(event) {
  if (!event.data) return {};
  try { return event.data.json() || {}; }
  catch { try { return { body: event.data.text() }; } catch { return {}; } }
}

self.addEventListener('push', event => {
  const data = readPayload(event);
  const title = String(data.title || 'nth');
  const options = {
    body: String(data.body || ''),
    // One tag per channel: a burst replaces the previous notification instead
    // of stacking. renotify still buzzes for the replacement.
    tag: String(data.tag || 'nth'),
    renotify: true,
    icon: '/icons/icon-192.png',
    badge: '/icons/badge-96.png',
    data: { url: String(data.url || '/'), channel: String(data.channel || '') },
  };
  // Safari and Chrome both require every push to show a notification
  // (userVisibleOnly), so this always shows one, even for a malformed payload.
  event.waitUntil(self.registration.showNotification(title, options));
});

function sameChannel(clientUrl, target) {
  try {
    const u = new URL(clientUrl);
    if (u.origin !== target.origin) return false;
    const want = target.searchParams.get('channel');
    return !want || u.searchParams.get('channel') === want;
  } catch { return false; }
}

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const data = event.notification.data || {};
  let target = new URL(data.url || '/', self.location.origin);
  // A notification only ever opens this app. An absolute URL to anywhere
  // else in the payload falls back to the app's home.
  if (target.origin !== self.location.origin) target = new URL('/', self.location.origin);
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const exact = windows.find(c => sameChannel(c.url, target));
    if (exact) return exact.focus();
    // The app is open on another view: bring it forward and let the page
    // switch channel itself (47-push.js listens for this), which keeps its
    // live connections instead of reloading.
    const any = windows.find(c => { try { return new URL(c.url).origin === target.origin; } catch { return false; } });
    if (any) {
      await any.focus();
      any.postMessage({ type: 'nth-open-channel', channel: data.channel || target.searchParams.get('channel') || '' });
      return undefined;
    }
    return self.clients.openWindow(target.href);
  })());
});
