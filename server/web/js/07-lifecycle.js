(() => {
  'use strict';
  const Trio = window.Trio = window.Trio || {};
  const mounted = new Map();
  const leaks = new Map();
  function mount(name, feature, services = []) {
    unmount(name);
    const cleanup = [];
    function onUnmount(fn) { cleanup.push(fn); }
    function track(kind, resource) {
      if (!leaks.has(name)) leaks.set(name, []);
      leaks.get(name).push({ kind, resource });
    }
    const ctx = { onUnmount, track, services };
    if (feature.mount) feature.mount(ctx); else if (feature.init) feature.init(ctx);
    mounted.set(name, { feature, cleanup });
    return ctx;
  }
  function unmount(name) {
    const entry = mounted.get(name);
    if (!entry) return;
    mounted.delete(name);
    leaks.delete(name);
    entry.cleanup.forEach(fn => { try { fn(); } catch (e) { console.warn('unmount error', name, e); } });
    if (entry.feature.unmount) { try { entry.feature.unmount(); } catch (e) { console.warn('unmount error', name, e); } }
  }
  function unmountAll() { for (const name of [...mounted.keys()]) unmount(name); }
  function reportLeaks() { const out = [...leaks.entries()].filter(([, v]) => v.length); if (out.length) console.warn('lifecycle leaks:', Object.fromEntries(out)); return out; }
  Trio.lifecycle = { mount, unmount, unmountAll, reportLeaks };
})();
