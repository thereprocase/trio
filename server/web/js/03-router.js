(() => {
  'use strict';
  const Trio = window.Trio = window.Trio || {};
  const store = Trio.store;
  const state = Trio.state;
  const handlers = new Set();
  const pageRoutes = {
    '/': 'home',
    '/inbox': 'attention',
    '/attention': 'attention',
    '/messages': 'messages',
    '/tasks': 'tasks',
    '/agents': 'roster',
    '/roster': 'roster',
    '/settings': 'prefs',
    '/preferences': 'prefs',
    '/archive': 'archive',
    '/data': 'data',
  };
  const pagePaths = {
    home: '/',
    attention: '/inbox',
    messages: '/messages',
    tasks: '/tasks',
    roster: '/agents',
    prefs: '/settings',
    archive: '/archive',
    data: '/data',
  };
  function parse(search = location.search, pathname = location.pathname || '/') {
    const q = new URLSearchParams(search);
    const dm = q.get('dm') || '';
    const channel = q.get('channel') || '';
    const archived = q.get('archived') === '1';
    const audit = q.get('audit') === '1';
    if (dm) return { name: audit ? 'audit' : 'dm', params: { key: dm, ...(archived && !audit ? { archived: true } : {}) }, title: 'DM ' + dm, readOnly: audit || archived };
    if (channel) return { name: 'channel', params: { code: channel, archived }, title: 'trio#' + channel, readOnly: archived };
    const name = pageRoutes[pathname] || 'home';
    return { name, params: {}, title: name === 'home' ? 'nth' : name, readOnly: false };
  }
  function serialize(route) {
    const params = route.params || {};
    if (route.name === 'dm' || route.name === 'audit') {
      const extra = route.name === 'audit' ? '&audit=1' : params.archived ? '&archived=1' : '';
      return '/?dm=' + encodeURIComponent(params.key) + extra;
    }
    if (route.name === 'channel') {
      const extra = params.archived ? '&archived=1' : '';
      return '/?channel=' + encodeURIComponent(params.code) + extra;
    }
    return pagePaths[route.name] || '/';
  }
  function apply(route) {
    const current = store ? store.getState() : state;
    current.route = route;
    if (store) store.set('route', route);
    handlers.forEach(fn => { try { fn(route); } catch (e) { console.warn('router handler error', e); } });
  }
  function navigate(name, params = {}, { replace = false, title = '' } = {}) {
    const route = { name, params, title: title || name, readOnly: name === 'audit' || params.archived };
    apply(route);
    const url = serialize(route);
    if (replace) history.replaceState(route, '', url); else history.pushState(route, '', url);
  }
  let popHandler, clickHandler;
  function init() {
    const route = parse(location.search, location.pathname || '/');
    apply(route);
    popHandler = event => { const r = event.state || parse(location.search, location.pathname || '/'); apply(r); };
    clickHandler = event => {
      const a = event.target.closest('a[data-route]');
      if (a) { event.preventDefault(); const [name, ...rest] = a.dataset.route.split(':'); const params = name === 'channel' ? { code: rest.join(':') } : { key: rest.join(':') }; navigate(name, params); }
    };
    addEventListener('popstate', popHandler);
    addEventListener('click', clickHandler);
  }
  function mount() { init(); }
  function unmount() { if (popHandler) removeEventListener('popstate', popHandler); if (clickHandler) removeEventListener('click', clickHandler); popHandler = clickHandler = null; }
  Trio.router = { init, mount, unmount, navigate, replace: (name, params) => navigate(name, params, { replace: true }), parse, serialize, on: (fn) => { handlers.add(fn); return () => handlers.delete(fn); }, current: () => (store ? store.getState().route : state.route) };
})();
