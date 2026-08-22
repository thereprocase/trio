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
//
// ── Fake-DOM supported surface ──────────────────────────────────────────────
// SUPPORTED (behaves like a real browser for these):
//   • element tree: appendChild/append/insertBefore/removeChild/remove/
//     replaceWith/replaceChildren, parentNode/parentElement, DocumentFragment
//     splicing on insert.
//   • classList add/remove/toggle/contains/replace; className get/set.
//   • textContent get/set; innerHTML get/set (set PARSES the well-formed,
//     renderMarkdown-shaped HTML into real nodes — nested tags, quoted/bare
//     attributes, void elements, entity-decoded text).
//   • querySelector/querySelectorAll for tag / .class / #id / ':scope >' /
//     descendant / comma-groups; closest() up the ancestor chain; matches().
//   • document.createElement/createTextNode/createDocumentFragment/
//     createTreeWalker (SHOW_TEXT/SHOW_ELEMENT); NodeFilter.
//   • style.setProperty/getProperty + arbitrary style.foo writes; dataset;
//     get/setAttribute; addEventListener (recorded, never fired).
// NOT SUPPORTED (deliberate gaps — do not trust a test that leans on these):
//   • document.getElementById AUTO-CREATES a <div> on miss and never returns
//     null; document.querySelector at the DOCUMENT level always returns null.
//     So `if (!document.getElementById(x))` / document-level queries are NOT
//     faithfully exercised — test such logic by extracting a pure helper
//     (the nth_ask_client.js pattern) instead.
//   • layout/geometry (getBoundingClientRect → zeros), scroll, focus, events
//     (listeners are stored but never dispatched), animation frames.
// GROWTH RULE: prefer pushing pure logic into a require()-able sibling module
// (like server/nth_ask_client.js) and testing it directly; reserve this
// harness for logic genuinely welded to the IIFE closure + DOM (paintBody,
// applyTargetBars, decorateInlineMentions). Grow the fake DOM by making a
// missing surface THROW a clear "harness gap" — never return a plausible-but-
// wrong value that paints a passing test green over untested code.
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const WEB_ROOT = path.resolve(__dirname, '..', 'server', 'web');
const ASK_JS = path.resolve(__dirname, '..', 'server', 'nth_ask_client.js');

// ── Fake DOM ──────────────────────────────────────────────────────────────
// A text node is the simplest thing that carries text. nodeValue mirrors
// textContent (real DOM aliases them for text nodes); replaceWith swaps this
// node out for one or more nodes / a fragment.
function textNode(text) {
  const node = {
    nodeType: 3,
    textContent: String(text),
    _parent: null,
    get nodeValue() { return this.textContent; },
    set nodeValue(v) { this.textContent = String(v); },
    get parentNode() { return this._parent; },
    get parentElement() { return this._parent; },
    replaceWith(...nodes) {
      if (this._parent) this._parent._replaceChild(this, nodes);
    },
  };
  return node;
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
    this.nodeType = 1;               // ELEMENT_NODE (fragments override to 11)
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
    // Textarea/input caret state. Real elements expose these as numbers, and
    // caret-aware code branches on that type — a stub that omitted them would
    // silently exercise only the no-caret fallback path.
    this.selectionStart = 0;
    this.selectionEnd = 0;
    this.classList = makeClassList(this);
    this._listeners = {};
    this.dispatched = [];        // event types passed to dispatchEvent()
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
  get parentElement() { return this._parent && this._parent.nodeType !== 11 ? this._parent : null; }
  get firstChild() { return this.children[0] || null; }
  get firstElementChild() { return this.children.find(c => c.nodeType !== 3) || null; }
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
  // Setting innerHTML parses the (well-formed, renderMarkdown-shaped) HTML into
  // a real child-node tree so tree-walking code — decorateInlineMentions —
  // actually runs. Getting it returns the raw string when set directly and not
  // since mutated, else serializes the tree back.
  get innerHTML() { return this._html != null ? this._html : serializeChildren(this); }
  set innerHTML(v) {
    this.children = [];
    this._html = String(v == null ? '' : v);
    parseHtmlInto(this, this._html);
  }

  // Flatten a fragment to its children (moved out of it); pass other nodes
  // through. Strings become text nodes.
  _toInsertable(node) {
    if (typeof node === 'string') return [textNode(node)];
    if (node && node.nodeType === 11) {           // DocumentFragment
      const kids = node.children.slice();
      kids.forEach(k => { k._parent = null; });
      node.children = [];
      return kids;
    }
    return [node];
  }
  _appendNode(node) {
    for (const n of this._toInsertable(node)) {
      if (n._parent) n._parent._detach(n);
      n._parent = this;
      this._html = null;             // real DOM: adding a node invalidates raw html
      this.children.push(n);
    }
    return node;
  }
  _detach(node) {
    const i = this.children.indexOf(node);
    if (i >= 0) this.children.splice(i, 1);
    node._parent = null;
  }
  // Replace `node` in this element's child list with `repl` (node/fragment/
  // array), preserving position. Backs text/element replaceWith().
  _replaceChild(node, repl) {
    const i = this.children.indexOf(node);
    if (i < 0) return;
    const list = (Array.isArray(repl) ? repl : [repl])
      .flatMap(r => this._toInsertable(r));
    list.forEach(n => { if (n._parent) n._parent._detach(n); n._parent = this; });
    this.children.splice(i, 1, ...list);
    node._parent = null;
    this._html = null;
  }
  appendChild(node) { return this._appendNode(node); }
  append(...nodes) { nodes.forEach(n => this._appendNode(n)); }
  insertBefore(node, ref) {
    const list = this._toInsertable(node);
    list.forEach(n => { if (n._parent) n._parent._detach(n); n._parent = this; });
    this._html = null;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(...list); else this.children.splice(i, 0, ...list);
    return node;
  }
  removeChild(node) { this._detach(node); return node; }
  remove() { if (this._parent) this._parent._detach(this); }
  replaceWith(...nodes) { if (this._parent) this._parent._replaceChild(this, nodes); }
  replaceChildren(...nodes) {
    this.children = []; this._html = null;
    nodes.forEach(n => this._appendNode(n));
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
  // Listeners are recorded, never fired (as everywhere else in this harness),
  // but the dispatched types are kept so a test can assert that code which
  // MUST notify a mirror / autosize / preview actually did.
  dispatchEvent(e) { this.dispatched.push((e && e.type) || ''); return true; }
  setSelectionRange(s, e) { this.selectionStart = s; this.selectionEnd = e; }

  focus() {} blur() {} click() {} scrollIntoView() {} showModal() {} close() {}
  getBoundingClientRect() { return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }; }
  // Nearest self-or-ancestor element matching `sel`. decorateInlineMentions
  // relies on this to skip @mentions already inside <code>/<pre>/<a>/.inline-
  // mention — a no-op stub would silently mis-decorate, so it's implemented.
  closest(sel) {
    let el = this;
    while (el && el.nodeType !== 3) {
      if (elementMatches(el, sel)) return el;
      el = el._parent;
    }
    return null;
  }
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

// ── minimal HTML (de)serialization ─────────────────────────────────────────
// Only as faithful as it needs to be for renderMarkdown's output: nested tags,
// double/single/bare attributes, void elements, and entity-decoded text. Not a
// spec HTML parser — it exists so tree-walking client code (decorateInline
// mentions) runs against real nodes instead of an opaque string.
const VOID_TAGS = new Set(['br', 'hr', 'img', 'input', 'meta', 'link', 'wbr']);
const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'", apos: "'", nbsp: ' ' };
function decodeEntities(s) {
  return s.replace(/&(#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);/g, (m, e) => {
    if (e[0] === '#') {
      const code = e[1] === 'x' || e[1] === 'X' ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
      return Number.isFinite(code) ? String.fromCodePoint(code) : m;
    }
    return Object.prototype.hasOwnProperty.call(ENTITIES, e) ? ENTITIES[e] : m;
  });
}
function parseAttrs(el, raw) {
  const re = /([a-zA-Z_:][-\w:.]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
  let m;
  while ((m = re.exec(raw))) {
    const name = m[1];
    const val = m[2] != null ? m[2] : m[3] != null ? m[3] : m[4] != null ? m[4] : '';
    el.setAttribute(name, decodeEntities(val));
    if (name === 'class') el.className = decodeEntities(val);
  }
}
// Populate `root`'s children by parsing `html`. Uses the shared FakeElement /
// textNode constructors (bound at load()).
let _makeEl = null;    // set once FakeElement is defined
function parseHtmlInto(root, html) {
  if (!html) return;
  const stack = [root];
  const top = () => stack[stack.length - 1];
  const re = /<\/?([a-zA-Z][\w-]*)((?:[^>"']|"[^"]*"|'[^']*')*?)\/?>/g;
  let last = 0, m;
  const pushText = (text) => {
    if (!text) return;
    top()._appendNode(textNode(decodeEntities(text)));
  };
  while ((m = re.exec(html))) {
    pushText(html.slice(last, m.index));
    last = re.lastIndex;
    const closing = m[0][1] === '/';
    const tag = m[1].toLowerCase();
    if (closing) {
      for (let i = stack.length - 1; i > 0; i--) {
        if (stack[i].tagName.toLowerCase() === tag) { stack.length = i; break; }
      }
      continue;
    }
    const el = _makeEl(tag);
    parseAttrs(el, m[2] || '');
    top()._appendNode(el);
    const selfClose = /\/>\s*$/.test(m[0]) || VOID_TAGS.has(tag);
    if (!selfClose) stack.push(el);
  }
  pushText(html.slice(last));
}
function serializeChildren(el) {
  return el.children.map(serializeNode).join('');
}
function serializeNode(node) {
  if (node.nodeType === 3) return escapeText(node.textContent);
  const tag = node.tagName.toLowerCase();
  let attrs = '';
  for (const [k, v] of node._attrs) attrs += ` ${k}="${escapeText(v)}"`;
  if (VOID_TAGS.has(tag)) return `<${tag}${attrs}>`;
  return `<${tag}${attrs}>${serializeChildren(node)}</${tag}>`;
}
function escapeText(s) {
  return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

// ── TreeWalker (SHOW_TEXT / SHOW_ELEMENT are all decorateInlineMentions uses) ─
const NodeFilter = { SHOW_ALL: -1, SHOW_ELEMENT: 1, SHOW_TEXT: 4, FILTER_ACCEPT: 1, FILTER_SKIP: 3 };
function createTreeWalker(root, whatToShow) {
  const show = whatToShow == null ? NodeFilter.SHOW_ALL : whatToShow;
  const flat = [];
  (function walk(node) {
    for (const c of node.children || []) {
      const bit = c.nodeType === 3 ? NodeFilter.SHOW_TEXT : NodeFilter.SHOW_ELEMENT;
      if (show === NodeFilter.SHOW_ALL || (show & bit)) flat.push(c);
      if (c.nodeType !== 3) walk(c);
    }
  })(root);
  let i = -1;
  return {
    currentNode: root,
    nextNode() { i++; if (i < flat.length) { this.currentNode = flat[i]; return flat[i]; } return null; },
  };
}

// Bind the element factory the HTML parser uses (FakeElement is defined above).
_makeEl = (tag) => new FakeElement(tag);

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
  const listeners = {};
  const doc = {
    _byId: byId,
    documentElement: new FakeElement('html'),
    createElement: (t) => new FakeElement(t),
    createDocumentFragment: () => { const f = new FakeElement('#fragment'); f.nodeType = 11; return f; },
    createTextNode: (t) => textNode(t),
    createTreeWalker: (root, whatToShow) => createTreeWalker(root, whatToShow),
    getElementById(id) {
      if (!byId.has(id)) { const el = new FakeElement('div'); el.id = id; byId.set(id, el); }
      return byId.get(id);
    },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    getElementsByClassName() { return []; },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    removeEventListener(type, fn) {
      const a = listeners[type] || []; const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1);
    },
    dispatchEvent(event) { (listeners[event.type] || []).slice().forEach(fn => fn(event)); return true; },
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
  constructor(url) { this.url = url; this.readyState = 0; this.closed = false; this._listeners = {}; FakeEventSource.instances.push(this); }
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  removeEventListener(type, fn) {
    const a = this._listeners[type] || []; const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1);
  }
  close() { this.closed = true; this.readyState = 2; }
  // Test helpers to drive the connection lifecycle the real EventSource fires.
  fireOpen() { this.readyState = 1; if (this.onopen) this.onopen(); }
  fireError() { if (this.onerror) this.onerror(); }
  fireMessage(data = '{}') { if (this.onmessage) this.onmessage({ data: String(data) }); }
  fireHeartbeat() { (this._listeners.heartbeat || []).slice().forEach(fn => fn({ type: 'heartbeat', data: '{}' })); }
}
FakeEventSource.instances = [];

function buildSandbox() {
  const document = makeDocument();
  const noop = () => {};
  const windowListeners = {};
  const window = {
    addEventListener(type, fn) { (windowListeners[type] = windowListeners[type] || []).push(fn); },
    removeEventListener(type, fn) {
      const a = windowListeners[type] || []; const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1);
    },
    dispatchEvent(event) { (windowListeners[event.type] || []).slice().forEach(fn => fn(event)); return true; },
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
    URL, URLSearchParams, TextEncoder, TextDecoder, NodeFilter,
    Map, Set, WeakMap, Promise, JSON, Math, Date, RegExp, Array, Object, String, Number, Boolean, Error,
    EventTarget, Event, CustomEvent,
    // Used by 20-workspace's search (it aborts the in-flight request on each
    // keystroke) and by 05-loader. Node has a real one; no need to stub.
    AbortController,
  });
  window.webkitAudioContext = window.AudioContext;
  window.__TRIO_TEST__ = {};        // truthy → nth_web.py's hook publishes helpers here
  return window;
}

// The module list is READ OUT OF nth_web.py, not written here. A hardcoded
// copy is a third list to keep in step with WEB_JS_FILES and setup.sh, and the
// way it fails is the worst kind: a module added to production but not to the
// harness escapes client coverage entirely, with every existing test still
// green. Parsing the tuple means the harness runs exactly what the server
// serves, and a reordering that breaks production breaks these tests too.
function productionModules() {
  const py = fs.readFileSync(path.resolve(__dirname, '..', 'server', 'nth_web.py'), 'utf8');
  const block = py.match(/WEB_JS_FILES = \(([\s\S]*?)\)/);
  if (!block) throw new Error('could not find WEB_JS_FILES in server/nth_web.py');
  const names = [...block[1].matchAll(/"js\/([^"]+)"/g)].map(m => m[1]);
  if (!names.length) throw new Error('WEB_JS_FILES parsed to an empty list');
  // The test hook is what the harness REPLACES below, so it must not also run.
  return names.filter(n => n !== '99-test-hook.js');
}

// Load the ordered source modules that production composes into INDEX_HTML.
function buildScript() {
  const all = productionModules();
  // 90-boot must stay last and must not auto-run: it mounts every feature
  // against a live DOM and opens an EventSource, which the tests drive
  // themselves. Asserted rather than assumed — if it ever stops being last in
  // WEB_JS_FILES, the harness would silently boot mid-load.
  if (all[all.length - 1] !== '90-boot.js') {
    throw new Error(`expected 90-boot.js last in WEB_JS_FILES, got ${all[all.length - 1]}`);
  }
  const files = all.slice(0, -1);
  return [
    fs.readFileSync(ASK_JS, 'utf8'),
    ...files.map(name => fs.readFileSync(path.join(WEB_ROOT, 'js', name), 'utf8')),
    "document.readyState = 'loading';",  // keep 90-boot from auto-running in the harness
    fs.readFileSync(path.join(WEB_ROOT, 'js', '90-boot.js'), 'utf8'),
  ].join('\n') + `
    globalThis.__TRIO_TEST__ = {
      state: window.Trio.state,
      renderMarkdown: window.Trio.markdown.renderMarkdown,
      escapeHtml: window.Trio.markdown.escapeHtml,
      isSystemContent: window.Trio.markdown.isSystemContent,
      systemMessageText: window.Trio.markdown.systemMessageText,
      humanizeIdSigils: window.Trio.markdown.humanizeIdSigils,
      paintBody: window.Trio.conversation.paintBody,
      cardFor: window.Trio.conversation.cardFor,
      answerPayload: window.Trio.conversation.answerPayload,
      isPrivate: window.Trio.conversation.isPrivate,
      buildSendPayload: window.Trio.composer.buildSendPayload,
      apiUrl: path => { const ch = window.Trio.state.channel || ''; return ch ? path + (path.includes('?') ? '&' : '?') + 'channel=' + encodeURIComponent(ch) : path; },
      rememberColors: () => {},
      applyTargetBars: () => {},
      targetableMembers: members => (members || []).filter(m => !m.is_operator),
      soleAgentId: members => { const ids = (members || []).filter(m => !m.is_operator).map(m => m.id); return ids.length === 1 ? ids[0] : null; },
      directAt: (text, m) => { const n = m && m.name; return n && !(new RegExp('(^|\\\\s)@' + n.replace(/[-/\\\\^\\x24*+?.()|[\\]{}]/g, '\\\\$&') + '(?=\\\\s|$)', 'i')).test(text) ? '@' + n + ' ' + text : text; },
      upsert: window.Trio.conversation.upsert,
      render: window.Trio.conversation.render,
      detectFilePathCandidates: window.Trio.fileLinks.detectFilePathCandidates,
      linkifyValidatedPaths: window.Trio.fileLinks.linkifyValidatedPaths,
      sidebar: window.Trio.sidebar,
      lightbox: window.Trio.lightbox,
      Trio: window.Trio,
    };
  `;
}

function load() {
  const sandbox = buildSandbox();
  const context = vm.createContext(sandbox);
  const script = buildScript();
  let bootError = null;
  try {
    vm.runInContext(script, context, { filename: 'trio-web-modules.js', timeout: 5000 });
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
