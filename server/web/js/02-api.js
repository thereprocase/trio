(() => {
  'use strict';
  const Trio = window.Trio = window.Trio || {};
  const CHANNEL_RE = /(^|&|\?)channel=/;
  const state = () => Trio.state || {};
  function url(path, channelScoped = true) {
    if (!channelScoped || !state().channel) return path;
    if (CHANNEL_RE.test(path)) return path;  // avoid duplicate channel params
    return path + (path.includes('?') ? '&' : '?') + 'channel=' + encodeURIComponent(state().channel);
  }
  // Turn a status + server detail into something worth showing a person.
  // The server's own `error` string is usually the best sentence available, so
  // it is preferred where it exists; these fill the gaps where the status is
  // the whole story and the detail is jargon or absent.
  const BY_STATUS = {
    401: 'You are not signed in to this workspace.',
    403: 'You are viewing as a guest — ask the hub owner to trust this device.',
    404: 'That is no longer here.',
    409: 'That is not enabled on this server.',
    413: 'That file is too large.',
    429: 'Too many requests just now — try again in a moment.',
    500: 'The server hit an error handling that.',
    503: 'The server is not ready yet — try again in a moment.',
  };
  function humanize(status, detail) {
    const clean = String(detail || '').trim();
    // A detail that is already a sentence beats a generic one. Reject the
    // shapes that are plainly machine text: bare paths, bare status echoes.
    const usable = clean && !/^\/api\//.test(clean) && !/^\d{3}\b/.test(clean)
      && clean.length > 3;
    if (usable) return clean.charAt(0).toUpperCase() + clean.slice(1);
    return BY_STATUS[status] || `That request failed (${status}).`;
  }

  // A viewer the hub cannot name over Tailscale (another tailnet's user, a
  // shared node) sits in the "pending" tier, and the server refuses its posts
  // until it declares a guest name through /api/identify. Nothing else in the
  // page asks for that name, so the first refused action asks for it here.
  const NEEDS_NAME = /pick a name/i;
  async function declareGuestName() {
    const raw = window.prompt('Pick a name to post in this channel (up to 40 characters):') || '';
    const name = raw.trim().slice(0, 40);
    if (!name) return false;
    const response = await fetch('/api/identify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    if (!response.ok) return false;
    const data = await response.json().catch(() => ({}));
    if (data.operator && Trio.state) {
      Trio.state.operator = data.operator;
      if (Trio.store) Trio.store.set('session.operator', data.operator);
    }
    return true;
  }

  async function request(method, path, body = null, channelScoped = true, options = {}) {
    const u = url(path, channelScoped);
    const init = { method, headers: {} };
    if (body !== null) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    if (options.signal) init.signal = options.signal;
    // Bound reads through body consumption too: suspended mobile connections
    // can return headers then stop delivering bytes. Never retry writes here.
    const controller = method === 'GET' ? new AbortController() : null;
    let timer, timedOut = false;
    const abort = () => controller?.abort();
    if (controller) {
      init.signal = controller.signal;
      if (options.signal?.aborted) abort();
      else options.signal?.addEventListener('abort', abort, {once:true});
      timer = setTimeout(() => { timedOut = true; controller.abort(); }, 15000);
    }
    let response, text;
    try {
      response = await fetch(u, init);
      text = await response.text();
    } catch (error) {
      if (timedOut) {
        const timeout = new Error('The server took too long to respond. Try again.');
        timeout.status = 408; timeout.path = path; throw timeout;
      }
      throw error;
    } finally {
      if (timer !== undefined) clearTimeout(timer);
      options.signal?.removeEventListener('abort', abort);
    }
    let data;
    try { data = text ? JSON.parse(text) : { ok: false }; } catch { data = { ok: false, error: text.trim() || 'Server returned non-JSON' }; }
    if (!response.ok) {
      const detail = data?.error || text || 'request failed';
      if (response.status === 403 && NEEDS_NAME.test(detail) && !options.nameDeclared
          && await declareGuestName()) {
        return request(method, path, body, channelScoped, { ...options, nameDeclared: true });
      }
      // Two audiences. ~20 call sites toast `error.message` verbatim, so the
      // message has to read as a sentence to a person — "403 /api/send: not a
      // trusted operator" is a stack trace shown to a human, and an upload
      // that fails the quota used to surface as bare "413 /api/upload" with no
      // size, no filename and no advice. `detail`/`status`/`path` stay on the
      // error for the console and for callers that want to branch.
      const err = new Error(humanize(response.status, detail));
      err.status = response.status;
      err.detail = detail;
      err.path = path;
      throw err;
    }
    if (data && data.ok === false) {
      const err = new Error(data.error || 'request failed');
      err.status = 500;
      throw err;
    }
    return data;
  }
  Trio.api = {
    url,
    // Exported for tests: a pure function, and the thing that decides what a
    // person actually reads when a request fails.
    humanize,
    get: (path, channelScoped = true, options = {}) => request('GET', path, null, channelScoped, options),
    post: (path, body, channelScoped = true, options = {}) => request('POST', path, body, channelScoped, options),
    // /api/upload takes the image bytes as the RAW BODY, with the name in an
    // X-Filename header — it does no multipart parsing at all. This helper
    // used to POST a FormData, which the server sniffs as "not a PNG/JPEG/GIF/
    // WebP" and rejects with 400. Nothing called it, so the disagreement was
    // invisible: the composer had its own correct copy of this request inline.
    // One upload path now, matching the server, and the composer uses it.
    upload: async (file, channelScoped = true) => {
      const response = await fetch(url('/api/upload', channelScoped), {
        method: 'POST',
        // Percent-encoded because HTTP headers are ISO-8859-1 and filenames
        // (macOS screenshots especially) carry Unicode.
        headers: { 'Content-Type': file.type,
                   'X-Filename': encodeURIComponent(file.name || 'image') },
        body: file,
      });
      if (!response.ok) {
        let detail = '';
        try { detail = (await response.json()).error || ''; } catch (e) { /* non-JSON */ }
        const err = new Error(humanize(response.status, detail));
        err.status = response.status;
        err.detail = detail;
        err.path = '/api/upload';
        throw err;
      }
      return response.json();
    },
  };
})();
