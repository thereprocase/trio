// Pure client-side helpers for the trio_ask picker / questionnaire.
//
// These are the shape-normalizing / text-composing functions with NO DOM
// dependency. They are injected verbatim into nth_web.py's INDEX_HTML (via the
// ASK_HELPERS placeholder) AND require()-able by Node so they can be
// unit-tested — the `isAskChoices` gate in particular shipped broken twice
// because the client JS had no automated coverage. Keep this file DOM-free.

// Normalize a `choices` payload to a list of question objects, tolerating both
// the batched shape ({target, questions:[…]}) and the legacy single shape
// ({target, question, options, mode}).
function askQuestions(choices) {
  if (choices && Array.isArray(choices.questions)) return choices.questions;
  if (choices && Array.isArray(choices.options)) {
    return [{ question: choices.question, options: choices.options, mode: choices.mode }];
  }
  return [];
}

// Is this message payload an interactive ask? This is the exact predicate the
// appendMessage render gate uses — a regression here silently turns every
// picker into plain text (the bug fixed in 75b99b3).
function isAskChoices(choices) {
  return askQuestions(choices).length > 0;
}

// Normalize a stored `selection` to a per-question answer list, tolerating the
// legacy single-answer shape ({picked, custom:string}).
function askAnswers(sel) {
  if (sel && Array.isArray(sel.answers)) return sel.answers;
  if (sel && (Array.isArray(sel.picked) || typeof sel.custom === 'string')) {
    return [{ picked: sel.picked || [], custom: sel.custom ? [sel.custom] : [] }];
  }
  return [];
}

// One question's answer string: selected option texts + typed answers, all
// comma-joined. Out-of-range picked indices are skipped; blank customs dropped.
function answerStringFor(q, picked, customList) {
  const parts = [];
  const opts = q && Array.isArray(q.options) ? q.options : [];
  for (const i of (picked || [])) { if (opts[i] !== undefined) parts.push(opts[i]); }
  for (const c of (customList || [])) { const t = (c || '').trim(); if (t) parts.push(t); }
  return parts.join(', ');
}

// The full answer text posted back to the channel. For a single question it's
// just the answer string; for a batch each question is prefixed for the agent.
// `answers` is [{picked:[int], custom:[str]}] aligned to `questions`.
function composeAnswer(questions, answers, multi) {
  return (questions || []).map((q, qi) => {
    const a = (answers && answers[qi]) || { picked: [], custom: [] };
    const s = answerStringFor(q, a.picked || [], a.custom || []);
    return multi ? ((q.question || ('Q' + (qi + 1))) + ' → ' + s) : s;
  }).join(multi ? '\n' : '');
}

// Requireable in Node for tests; harmless inline in the browser (module is
// undefined there, so the guard is false).
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { askQuestions, isAskChoices, askAnswers, answerStringFor, composeAnswer };
}
