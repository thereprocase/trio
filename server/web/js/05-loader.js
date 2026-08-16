(() => {
  'use strict';
  const Trio = window.Trio = window.Trio || {};
  const active = new Map();
  function load(name, fn) {
    cancel(name);
    const controller = new AbortController();
    active.set(name, controller);
    const run = fn(controller.signal);
    run.finally(() => { if (active.get(name) === controller) active.delete(name); });
    return run;
  }
  function cancel(name) {
    const controller = active.get(name);
    if (controller) { controller.abort(); active.delete(name); }
  }
  function isActive(name) { return active.has(name); }
  function cancelAll(prefix = '') {
    for (const [name, controller] of active) { if (name.startsWith(prefix)) { controller.abort(); } }
  }
  Trio.loader = { load, cancel, isActive, cancelAll };
})();
