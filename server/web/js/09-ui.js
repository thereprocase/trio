(() => {
  'use strict';
  const Trio = window.Trio = window.Trio || {};
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const configuredDialogs = new WeakSet();
  function configureDialog(node) {
    if (!node || configuredDialogs.has(node)) return node;
    configuredDialogs.add(node);
    node.addEventListener('click', event => {
      if (event.target === node && node.open) node.close();
    });
    return node;
  }
  // Optional `action` = { label, onClick } renders an inline button (e.g. an
  // "Undo" affordance) that runs onClick and dismisses the toast. Back-compat:
  // existing callers pass no action and get a plain text toast.
  function toast(message, timeout = 3500, action = null) {
    let host = document.getElementById('trio-toasts');
    if (!host) { host = document.createElement('div'); host.id = 'trio-toasts'; host.className = 'toast-wrap'; document.body.append(host); }
    // Promote the host to the top layer so toasts paint ABOVE an open modal
    // <dialog> — the dialog and its blurred ::backdrop live in the top layer,
    // which no normal-DOM element can paint over at any z-index. This must run
    // UNCONDITIONALLY: the host also ships statically in index.html, so gating
    // it on host creation left that static host un-promoted and showPopover()
    // then threw (swallowed), leaving the toast stuck behind the blur.
    if (!host.hasAttribute('popover')) host.setAttribute('popover', 'manual');
    const node = document.createElement('div'); node.className = 'toast'; node.textContent = message; host.append(node);
    const dismiss = () => { node.remove(); if (!host.childElementCount) { try { host.hidePopover?.(); } catch {} } };
    if (action && action.label && typeof action.onClick === 'function') {
      const btn = document.createElement('button'); btn.type = 'button'; btn.className = 'toast-action'; btn.textContent = action.label;
      btn.addEventListener('click', () => { action.onClick(); dismiss(); });
      node.append(btn);
    }
    // showPopover throws if already open — swallow that and keep appending.
    if (host.showPopover) { try { host.showPopover(); } catch {} }
    setTimeout(dismiss, timeout);
  }
  // `body` is raw HTML, unlike `title` — callers MUST pre-escape any
  // user-controlled content (via `esc()` here or `Trio.markdown.escapeHtml`)
  // before passing it in.
  function modal(title, body, submit, options = {}) {
    let node = document.getElementById('trio-control-modal');
    if (!node) { node = document.createElement('dialog'); node.id = 'trio-control-modal'; document.body.append(node); }
    configureDialog(node);
    const cancelLabel = esc(options.cancelLabel || 'Cancel');
    // The accept button has to be able to name its action. Hardcoding "Save"
    // meant every destructive confirmation in the app was accepted by a button
    // reading Save — "Permanently delete the entire 'deploy' channel? …
    // This cannot be undone." with a blue Save underneath. The verb in the
    // prompt and the verb on the button never agreed, on any dialog.
    const submitLabel = esc(options.submitLabel || 'Save');
    const danger = options.danger ? ' danger' : '';
    const submitButton = options.submit === false ? ''
      : `<button type="submit" value="default" class="primary${danger}">${submitLabel}</button>`;
    // The × and Cancel buttons are type="button" close triggers, NOT submits.
    // Previously they were submit buttons, and the × (first in tree order) was
    // therefore the form's implicit-submission default — so pressing Enter in a
    // field cancelled the dialog. With only Save left as a submit button, Enter
    // now accepts the dialog (e.g. creates the channel) as expected.
    node.innerHTML = `<form method="dialog" class="control-modal"><button type="button" class="modal-close" data-close aria-label="Close">×</button><h2>${esc(title)}</h2>${body}<footer><button type="button" data-close>${cancelLabel}</button>${submitButton}</footer></form>`;
    node.querySelectorAll('[data-close]').forEach(btn => btn.addEventListener('click', () => node.close('cancel')));
    node.addEventListener('close', () => { if (node.returnValue === 'default') submit?.(node); }, { once: true }); node.showModal();
  }
  function confirmAction(message, actionOrDescription, maybeAction, options = {}) {
    // Two-arg form: confirmAction(message, action)
    // Three-arg form: confirmAction(message, description, action) — renders a
    // body line under the prompt; description is text-only (escaped here).
    // Either form takes a trailing options object; pass {submitLabel: 'Delete',
    // danger: true} so the button says what it will do.
    const hasDescription = typeof actionOrDescription === 'string';
    const description = hasDescription ? actionOrDescription : '';
    const action = hasDescription ? maybeAction : actionOrDescription;
    const opts = hasDescription ? (options || {})
                                : (maybeAction || options || {});
    const body = description
      ? `<p>${esc(message)}</p><p class="modal-desc">${esc(description)}</p>`
      : `<p>${esc(message)}</p>`;
    // Default the button to the FIRST WORD of the prompt ("Archive this
    // channel?" -> "Archive"), so a caller that says nothing still gets a
    // button that agrees with its question instead of a generic Save.
    const inferred = /^([A-Z][a-z]+)\b/.exec(String(message || ''));
    const submitLabel = opts.submitLabel || (inferred ? inferred[1] : 'Confirm');
    modal('Confirm', body, () => action?.(),
          { submitLabel, danger: opts.danger });
  }
  function setLive(text) {
    let region = document.getElementById('trio-aria-live');
    if (!region) { region = document.createElement('div'); region.id = 'trio-aria-live'; region.setAttribute('aria-live', 'polite'); region.setAttribute('aria-atomic', 'true'); region.className = 'sr-only'; document.body.append(region); }
    region.textContent = text;
  }
  // Clipboard write with a fallback for non-secure contexts. navigator.clipboard
  // is only defined in secure contexts (https or localhost); when the dashboard
  // is served over plain http to a tailnet peer (`nth_web.py --tailnet`) it is
  // undefined, so fall back to the legacy execCommand path. Generic (no
  // conversation coupling), so it lives here for any surface that needs a copy.
  function copyText(text) {
    const value = text == null ? '' : String(text);
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(value);
    return new Promise((resolve, reject) => {
      try {
        const ta = document.createElement('textarea');
        ta.value = value; ta.setAttribute('readonly', '');
        ta.style.position = 'fixed'; ta.style.top = '-1000px'; ta.style.opacity = '0';
        document.body.append(ta); ta.select();
        const ok = document.execCommand('copy'); ta.remove();
        ok ? resolve() : reject(new Error('copy command rejected'));
      } catch (err) { reject(err); }
    });
  }
  Trio.ui = { toast, modal, confirmAction, configureDialog, setLive, copyText };
})();
