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
    upload: async (file, channelScoped = true) => {
      const form = new FormData();
      form.append('file', file);
      const response = await fetch(url('/api/upload', channelScoped), { method: 'POST', body: form });
      if (!response.ok) throw new Error(`${response.status} /api/upload`);
      return response.json();
    },
  };
})();
