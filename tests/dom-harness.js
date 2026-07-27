// Minimal, dependency-free DOM/BOM harness for the trio web client.
//
// The dashboard's client logic lives inside one big IIFE embedded in
// nth_web.py's INDEX_HTML. That code has real branching worth testing —
// markdown rendering, system-line detection, id-sigil humanizing, message
// repaint — but it's private to the IIFE closure and assumes a browser.
//
// This harness runs the ACTUAL shipped script (no source fork) inside a Node
// `vm` sandbox against a hand-rolled fake DOM/BOM, then reads the internal
// helpers back out through the browser-inert __TRIO_TEST__ hook that nth_web.py
// exposes only when this harness pre-seeds it. Node stdlib only — no jsdom,
// no npm, matching the repo's zero-dependency philosophy.
//
// Usage:
//   const { load } = require('./dom-harness');
//   const cx = load();                 // { hooks, context, document, window }
//   cx.hooks.renderMarkdown('**hi**'); // exercise a real client helper
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const WEB_PY = path.resolve(__dirname, '..', 'server', 'nth_web.py');
const ASK_JS = path.resolve(__dirname, '..', 'server', 'nth_ask_client.js');

// ── Fake DOM ──────────────────────────────────────────────────────────────
// A text node is the simplest thing that carries text.
function textNode(text) {
  return { nodeType: 3, textContent: String(text), _parent: null };
}

// classList backed by a Set kept in sync with element.className.
function makeClassList(el) {
  function sync() { el._className = Array.from(el._classes).join(' '); }
  return {
    add(...cs) { cs.forEach(c => c && el._classes.add(c)); sync(); },
    remove(...cs) { cs.forEach(c => el._classes.delete(c)); sync(); },
    toggle(c, force) {
      const has = el._classes.has(c);
      const want = force === undefined ? !has : !!force;
      if (want) el._classes.add(c); else el._classes.delete(c);
      sync();
      return want;
    },
    contains(c) { return el._classes.has(c); },
    replace(a, b) { if (el._classes.delete(a)) { el._classes.add(b); sync(); return true; } return false; },
    get length() { return el._classes.size; },
  };
}

class FakeElement {
  constructor(tag) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.id = '';
    this._classes = new Set();
    this._className = '';
    this.children = [];          // element + text nodes, in order
    this._parent = null;
    this._attrs = new Map();
    this._html = null;           // set only via innerHTML=
    this.dataset = {};
    this.style = makeStyle();
    this.value = '';
    this.checked = false;
    this.disabled = false;
    this.options = [];
    this.classList = makeClassList(this);
    this._listeners = {};
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    this.offsetHeight = 0;
  }
  get className() { return this._className; }
  set className(v) {
    this._className = String(v || '');
    this._classes = new Set(this._className.split(/\s+/).filter(Boolean));
  }
  get parentNode() { return this._parent; }
  get firstChild() { return this.children[0] || null; }
  get childNodes() { return this.children; }

  get textContent() {
    return this.children.map(c =>
      c.nodeType === 3 ? c.textContent : (c.textContent || '')).join('');
  }
  set textContent(v) {
    this.children = [];
    this._html = null;
    if (v !== '' && v != null) this._appendNode(textNode(v));
  }
  get innerHTML() { return this._html != null ? this._html : this.textContent; }
  set innerHTML(v) { this.children = []; this._html = String(v == null ? '' : v); }

  _appendNode(node) {
    if (node._parent) node._parent._detach(node);
    node._parent = this;
    this._html = null;               // real DOM: adding a node invalidates raw html
    this.children.push(node);
    return node;
  }
  _detach(node) {
    const i = this.children.indexOf(node);
    if (i >= 0) this.children.splice(i, 1);
    node._parent = null;
  }
  appendChild(node) { return this._appendNode(node); }
  append(...nodes) { nodes.forEach(n => this._appendNode(typeof n === 'string' ? textNode(n) : n)); }
  insertBefore(node, ref) {
    if (node._parent) node._parent._detach(node);
    node._parent = this;
    this._html = null;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(node); else this.children.splice(i, 0, node);
    return node;
  }
  removeChild(node) { this._detach(node); return node; }
  remove() { if (this._parent) this._parent._detach(this); }
  replaceChildren(...nodes) {
    this.children = []; this._html = null;
    nodes.forEach(n => this._appendNode(typeof n === 'string' ? textNode(n) : n));
  }

  setAttribute(k, v) {
    this._attrs.set(k, String(v));
    if (k === 'class') this.className = v;
    if (k === 'id') this.id = String(v);
  }
  getAttribute(k) { return this._attrs.has(k) ? this._attrs.get(k) : null; }
  hasAttribute(k) { return this._attrs.has(k); }
  removeAttribute(k) { this._attrs.delete(k); }

  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  removeEventListener(type, fn) {
    const a = this._listeners[type]; if (!a) return;
    const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1);
  }
  dispatchEvent() { return true; }

  focus() {} blur() {} click() {} scrollIntoView() {}
  getBoundingClientRect() { return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }; }
  closest() { return null; }
  matches(sel) { return elementMatches(this, sel); }
  contains(node) {
    if (node === this) return true;
    return this.children.some(c => c.nodeType !== 3 && c.contains && c.contains(node));
  }

  // Descendant search. `:scope > sel` restricts to direct children.
  querySelector(sel) { const r = this.querySelectorAll(sel); return r[0] || null; }
  querySelectorAll(sel) {
    const groups = String(sel).split(',').map(s => s.trim()).filter(Boolean);
    const out = [];
    for (const g of groups) collectMatches(this, g, out);
    return out;
  }
}

function makeStyle() {
  const store = {};
  const style = {
    setProperty(k, v) { store[k] = v; },
    getPropertyValue(k) { return store[k] || ''; },
    removeProperty(k) { delete store[k]; },
    _store: store,
  };
  // Allow arbitrary style.foo = 'bar' writes without throwing.
  return new Proxy(style, {
    set(t, k, v) { t[k] = v; return true; },
    get(t, k) { return k in t ? t[k] : ''; },
  });
}

// ── selector matching (minimal: tag, .class, #id, ':scope >' direct-child) ──
function parseSimple(tok) {
  // Returns predicate for one compound token like "div.foo" / ".bar" / "#id".
  const m = tok.match(/^([a-zA-Z][\w-]*)?((?:[.#][\w-]+)*)$/);
  if (!m) return () => false;
  const tag = m[1] ? m[1].toUpperCase() : null;
  const rest = m[2] || '';
  const classes = (rest.match(/\.[\w-]+/g) || []).map(s => s.slice(1));
  const idm = rest.match(/#([\w-]+)/);
  const id = idm ? idm[1] : null;
  return (el) => {
    if (el.nodeType === 3) return false;
    if (tag && el.tagName !== tag) return false;
    if (id && el.id !== id) return false;
    for (const c of classes) if (!el._classes.has(c)) return false;
    return true;
  };
}

function elementMatches(el, sel) {
  return String(sel).split(',').some(s => {
    s = s.trim().replace(/^:scope\s*>?\s*/, '');
    return parseSimple(s)(el);
  });
}

// Collect descendants of `root` matching a single (possibly combinatored)
// selector group. Supports "A B" (descendant) and "A > B" (child); a leading
// ":scope >" means "direct children of root".
function collectMatches(root, group, out) {
  const scoped = /^:scope\b/.test(group);
  group = group.replace(/^:scope\s*/, '');
  // Split into steps with combinators. We only need the final token's matcher
  // plus a child/descendant flag relative to root for the shapes used in code.
  const childOnly = /^>\s*/.test(group);
  const token = group.replace(/^>\s*/, '').trim();
  const pred = parseSimple(token);
  const scan = (node, directOnly) => {
    for (const c of node.children) {
      if (c.nodeType === 3) continue;
      if (pred(c)) out.push(c);
      if (!directOnly) scan(c, false);
    }
  };
  scan(root, scoped || childOnly);
}

// ── document / window ───────────────────────────────────────────────────────
function makeDocument() {
  const byId = new Map();
  const doc = {
    _byId: byId,
    documentElement: new FakeElement('html'),
    createElement: (t) => new FakeElement(t),
    createDocumentFragment: () => new FakeElement('#fragment'),
    createTextNode: (t) => textNode(t),
    getElementById(id) {
      if (!byId.has(id)) { const el = new FakeElement('div'); el.id = id; byId.set(id, el); }
      return byId.get(id);
    },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    getElementsByClassName() { return []; },
    addEventListener() {}, removeEventListener() {},
    hidden: false,
    visibilityState: 'visible',
    readyState: 'complete',
    cookie: '',
    title: 'nth_web',
    activeElement: null,
    execCommand() { return true; },
  };
  doc.body = new FakeElement('body');
  doc.head = new FakeElement('head');
  return doc;
}

function makeLocalStorage() {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
    clear: () => m.clear(),
  };
}

class FakeEventSource {
  constructor() { this.readyState = 0; }
  addEventListener() {} removeEventListener() {} close() {}
}

function buildSandbox() {
  const document = makeDocument();
  const noop = () => {};
  const window = {
    addEventListener: noop, removeEventListener: noop,
    matchMedia: () => ({ matches: false, addEventListener: noop, removeEventListener: noop, addListener: noop }),
    localStorage: makeLocalStorage(),
    location: { href: 'http://localhost/', origin: 'http://localhost', hostname: 'localhost', protocol: 'http:', search: '', pathname: '/' },
    navigator: { userAgent: 'node-harness', clipboard: { writeText: () => Promise.resolve() }, mediaDevices: {} },
    devicePixelRatio: 1,
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
    cancelAnimationFrame: noop,
    scrollTo: noop,
    fetch: () => new Promise(() => {}),   // never resolves; boot()'s network calls hang harmlessly
    EventSource: FakeEventSource,
    Notification: function () {},
    AudioContext: function () { return { createOscillator: () => ({ connect: noop, start: noop, stop: noop }), createGain: () => ({ connect: noop, gain: {} }), destination: {}, currentTime: 0 }; },
  };
  window.Notification.permission = 'default';
  window.Notification.requestPermission = () => Promise.resolve('default');
  window.window = window;
  window.self = window;
  window.globalThis = window;
  window.document = document;
  Object.assign(window, {
    console, setTimeout, clearTimeout, setInterval, clearInterval,
    URL, URLSearchParams, TextEncoder, TextDecoder,
    Map, Set, WeakMap, Promise, JSON, Math, Date, RegExp, Array, Object, String, Number, Boolean, Error,
  });
  window.webkitAudioContext = window.AudioContext;
  window.__TRIO_TEST__ = {};        // truthy → nth_web.py's hook publishes helpers here
  return window;
}

// Extract the embedded client <script> and inject the pure ask helpers, the
// same substitution nth_web.py performs at serve time.
function buildScript() {
  const py = fs.readFileSync(WEB_PY, 'utf8');
  const start = py.indexOf('<script>');
  const end = py.indexOf('</script>', start);
  if (start < 0 || end < 0) throw new Error('could not locate <script> block in nth_web.py');
  let js = py.slice(start + '<script>'.length, end);
  const askHelpers = fs.readFileSync(ASK_JS, 'utf8');
  // Same placeholder substitutions nth_web.py performs at serve time. The
  // animal lists only feed the guest-avatar picker; empty arrays parse fine
  // and don't affect any code path under test.
  js = js
    .replace('/*__ANIMAL_EMOJIS__*/', '[]')
    .replace('/*__ANIMAL_NAMES__*/', '[]')
    .replace('/*__ASK_HELPERS__*/', askHelpers);
  return js;
}

function load() {
  const sandbox = buildSandbox();
  const context = vm.createContext(sandbox);
  const script = buildScript();
  let bootError = null;
  try {
    vm.runInContext(script, context, { filename: 'nth_web.client.js', timeout: 5000 });
  } catch (e) {
    // boot() may throw against the minimal DOM — the __TRIO_TEST__ hook is set
    // BEFORE boot() runs, so the helpers are already published regardless.
    bootError = e;
  }
  const hooks = sandbox.__TRIO_TEST__;
  if (!hooks || !hooks.renderMarkdown) {
    throw new Error('client test hook not published' + (bootError ? ' (boot error: ' + bootError.message + ')' : ''));
  }
  return { hooks, context, document: sandbox.document, window: sandbox, bootError };
}

module.exports = { load, FakeElement, textNode };
