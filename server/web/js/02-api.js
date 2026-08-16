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
  async function request(method, path, body = null, channelScoped = true, options = {}) {
    const u = url(path, channelScoped);
    const init = { method, headers: {} };
    if (body !== null) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    if (options.signal) init.signal = options.signal;
    const response = await fetch(u, init);
    const text = await response.text();
    let data;
    try { data = text ? JSON.parse(text) : { ok: false }; } catch { data = { ok: false, error: text.trim() || 'Server returned non-JSON' }; }
    if (!response.ok) {
      const detail = data?.error || text || 'request failed';
      const err = new Error(`${response.status} ${path}: ${detail}`);
      err.status = response.status;
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
        const err = new Error(`${response.status} /api/upload${detail ? ': ' + detail : ''}`);
        err.status = response.status;
        throw err;
      }
      return response.json();
    },
  };
})();
